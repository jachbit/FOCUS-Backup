"""F.O.C.U.S.(c) Backup v1.1 - Jachbit 2026 - MIT License

Windows app around robocopy: the first run copies everything, later runs copy only
new / changed files. The dashboard runs on a local-only server and is shown in its
own app window (pywebview + Microsoft Edge WebView2).
"""
import ctypes
import json
import os
import re
import socket
import string
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

VERSION = "1.1"
FROZEN = getattr(sys, "frozen", False)
WINDOW = {"w": None}
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
# as an .exe: settings live next to the .exe, the page is unpacked to a temp folder
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
TEMPLATE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "templates"
CONFIG_PATH = APP_DIR / "config.json"
CACHE_PATH = APP_DIR / "history_cache.json"

DEFAULTS = {
    "source": "",
    "dest_folder": "",
    "overdue_days": 7,
    "skip_rebuildable": True,
    "include_secrets": False,
    "mirror": False,
    "precheck": True,
    "last_drive": "",
    "recent_sources": [],
    "port": 8139,
}

REBUILDABLE_DIRS = ["node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".vite"]
ALWAYS_SKIP_FILES = ["Thumbs.db", "desktop.ini", "*.tmp", "~$*"]
SECRET_FILES = [".env", "*.key", "*.pem", "*.token"]

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))


# ---------------------------------------------------------------- config ----
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def dest_names(cfg):
    base = cfg["dest_folder"].strip() or (Path(cfg["source"]).name + "_Backup")
    return base, base + "_logs"


# ----------------------------------------------------------------- disks ----
def list_disks(cfg):
    k32 = ctypes.windll.kernel32
    src_drive = (cfg["source"][:2] or "").upper()
    sys_drive = os.environ.get("SystemDrive", "C:").upper()
    mask = k32.GetLogicalDrives()
    disks = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        dev = f"{letter}:"
        root = dev + "\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3) or dev in (src_drive, sys_drive):
            continue
        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        if not k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free)) or total.value == 0:
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        backup_dir, _ = dest_names(cfg)
        disks.append({
            "drive": dev,
            "label": label.value or "(no name)",
            "fs": fs.value,
            "type": "USB / removable" if dtype == 2 else "disk",
            "free_gb": round(free.value / 1024 ** 3, 1),
            "total_gb": round(total.value / 1024 ** 3, 1),
            "has_backup": cfg["source"] != "" and os.path.isdir(os.path.join(root, backup_dir)),
        })
    return disks


# ------------------------------------------------------------- robocopy -----
def robocopy_args(cfg, dest, dry_run):
    args = ["robocopy", cfg["source"], dest,
            "/MIR" if cfg["mirror"] else "/E",
            "/COPY:DAT", "/DCOPY:T", "/FFT", "/XJ", "/MT:16", "/R:2", "/W:2",
            "/NP", "/NDL", "/BYTES"]
    if dry_run:
        args.append("/L")
    if cfg["skip_rebuildable"]:
        args += ["/XD"] + REBUILDABLE_DIRS
    xf = list(ALWAYS_SKIP_FILES)
    if cfg["skip_rebuildable"]:
        xf.append("*.pyc")
    if not cfg["include_secrets"]:
        xf += SECRET_FILES
    args += ["/XF"] + xf
    return args


SUMMARY_RE = re.compile(r"^\s*(Dirs|Files|Bytes)\s*:\s*(.+)$")
FILE_RE = re.compile(r"^\s*(New File|Newer|Older|Changed|Tweaked|Modified)\s+(\d+)\s+(.+)$")
ERROR_RE = re.compile(r"ERROR \d+ \(0x[0-9A-Fa-f]+\) Copying File (.+)$")
STARTED_RE = re.compile(r"^\s*Started\s*:\s*(.+)$")
ENDED_RE = re.compile(r"^\s*Ended\s*:\s*(.+)$")
OPTIONS_RE = re.compile(r"^\s*Options\s*:\s*(.+)$")


UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}


def parse_summary_line(rest):
    # robocopy logs written without /BYTES show "6.311 g" instead of raw bytes
    tokens = rest.split()
    nums, i = [], 0
    while i < len(tokens):
        try:
            val = float(tokens[i])
        except ValueError:
            return None
        if i + 1 < len(tokens) and tokens[i + 1].lower() in UNITS:
            val *= UNITS[tokens[i + 1].lower()]
            i += 1
        nums.append(int(val))
        i += 1
    keys = ["total", "copied", "skipped", "mismatch", "failed", "extras"]
    return dict(zip(keys, nums)) if len(nums) >= 5 else None


def parse_robo_date(text):
    try:
        return datetime.strptime(text.strip(), "%A, %B %d, %Y %I:%M:%S %p")
    except ValueError:
        return None


# 2 / 3 = the backup also holds files or folders since deleted from the source (kept on purpose)
RC_TEXT = {
    0: "Up to date - nothing new to copy",
    1: "New / changed files copied",
    2: "Up to date - nothing new to copy",
    3: "New / changed files copied",
}


def rc_text(rc):
    if rc is None:
        return "Stopped before it finished"
    if rc >= 8:
        return "Some files could not be copied"
    return RC_TEXT.get(rc, "Files copied")


# ------------------------------------------------------------ run state -----
STATE_LOCK = threading.Lock()
STATE = {"running": False}
PROC = {"p": None, "stop": False}


def set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def run_worker(cfg, drive, dry_run):
    backup_dir, logs_dir = dest_names(cfg)
    dest = os.path.join(drive + "\\", backup_dir)
    log_dir = os.path.join(drive + "\\", logs_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"backup_{datetime.now():%Y%m%d_%H%M%S}.log")
    started = time.time()

    files_total = bytes_total = None
    if cfg["precheck"]:
        set_state(phase="check")
        scan = subprocess.Popen(robocopy_args(cfg, dest, True) + ["/NFL", "/NJH"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="oem", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW)
        PROC["p"] = scan
        out, _ = scan.communicate()
        for line in out.splitlines():
            m = SUMMARY_RE.match(line)
            if m and m.group(1) in ("Files", "Bytes"):
                s = parse_summary_line(m.group(2))
                if s and m.group(1) == "Files":
                    files_total = s["copied"]
                elif s:
                    bytes_total = s["copied"]
        if PROC["stop"]:
            finish(None, started, log_path, dry_run, {})
            return

    set_state(phase="copy", files_total=files_total, bytes_total=bytes_total,
              check_seconds=round(time.time() - started))
    args = robocopy_args(cfg, dest, dry_run) + ["/TEE", f"/LOG+:{log_path}"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="oem", errors="replace",
                         creationflags=subprocess.CREATE_NO_WINDOW)
    PROC["p"] = p
    files_done = bytes_done = 0
    failed = []
    recent = []
    summary = {}
    for line in p.stdout:
        m = FILE_RE.match(line)
        if m:
            files_done += 1
            bytes_done += int(m.group(2))
            recent = (recent + [m.group(3).strip()])[-6:]
            set_state(files_done=files_done, bytes_done=bytes_done, recent=recent)
            continue
        e = ERROR_RE.search(line)
        if e and e.group(1).strip() not in failed:
            failed.append(e.group(1).strip())
            set_state(failed_files=failed[-20:])
            continue
        s = SUMMARY_RE.match(line)
        if s:
            summary[s.group(1).lower()] = parse_summary_line(s.group(2))
    rc = p.wait()
    finish(None if PROC["stop"] else rc, started, log_path, dry_run, summary)


def finish(rc, started, log_path, dry_run, summary):
    PROC["p"] = None
    set_state(running=False, phase="done", rc=rc, rc_text=rc_text(rc),
              seconds=round(time.time() - started), summary=summary, log=log_path)


# --------------------------------------------------------------- history ----
LOG_CACHE = {}


def parse_log(path):
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    if key in LOG_CACHE:
        return LOG_CACHE[key]
    runs, cur = [], None
    with open(path, encoding="mbcs", errors="replace") as fh:
        for line in fh:
            if cur is None or (cur.get("ended") and STARTED_RE.match(line)):
                if STARTED_RE.match(line):
                    cur = {"log": os.path.basename(path), "test": False, "summary": {}}
                    runs.append(cur)
                    cur["started"] = STARTED_RE.match(line).group(1).strip()
                continue
            if "started" not in cur:
                continue
            m = OPTIONS_RE.match(line)
            if m:
                cur["test"] = " /L " in f" {m.group(1)} "
                cur["mirror"] = "/MIR" in m.group(1)
                continue
            m = SUMMARY_RE.match(line)
            if m:
                cur["summary"][m.group(1).lower()] = parse_summary_line(m.group(2))
                continue
            m = ENDED_RE.match(line)
            if m:
                cur["ended"] = m.group(1).strip()
                continue
            if ERROR_RE.search(line):
                cur["errors"] = cur.get("errors", 0) + 1
    out = []
    for r in runs:
        s, e = parse_robo_date(r["started"]), parse_robo_date(r.get("ended", ""))
        files = r["summary"].get("files") or {}
        byts = r["summary"].get("bytes") or {}
        out.append({
            "log": r["log"],
            "started": s.isoformat() if s else r["started"],
            "started_ts": s.timestamp() if s else st.st_mtime,
            "seconds": round((e - s).total_seconds()) if s and e else None,
            "mode": "Test run" if r["test"] else ("Mirror" if r.get("mirror") else "Backup"),
            "finished": bool(r.get("ended")),
            "files_total": files.get("total"),
            "files_copied": files.get("copied"),
            "files_skipped": files.get("skipped"),
            "files_failed": files.get("failed"),
            "bytes_total": byts.get("total"),
            "bytes_copied": byts.get("copied"),
        })
    LOG_CACHE[key] = out
    return out


def read_history(cfg, drive):
    _, logs_dir = dest_names(cfg)
    log_dir = os.path.join(drive + "\\", logs_dir)
    runs = []
    if os.path.isdir(log_dir):
        for name in os.listdir(log_dir):
            if name.lower().endswith(".log"):
                try:
                    runs += parse_log(os.path.join(log_dir, name))
                except OSError:
                    pass
    runs.sort(key=lambda r: r["started_ts"], reverse=True)
    return runs


def load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(drive, label, runs):
    cache = load_cache()
    cache[drive] = {"label": label, "saved": datetime.now().isoformat(), "runs": runs[:200]}
    try:
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------- routes ---
@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    cfg = load_config()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        if "source" in data:
            src = str(data["source"]).strip().rstrip("\\")
            if not os.path.isdir(src):
                return jsonify({"ok": False, "error": f"Folder not found: {src}"}), 400
            cfg["source"] = src
            recent = [s for s in cfg.get("recent_sources", []) if s.lower() != src.lower()]
            cfg["recent_sources"] = ([src] + recent)[:8]
        if "dest_folder" in data:
            name = str(data["dest_folder"]).strip()
            if re.search(r'[\\/:*?"<>|]', name):
                return jsonify({"ok": False, "error": "Backup folder name cannot contain \\ / : * ? \" < > |"}), 400
            cfg["dest_folder"] = name
        if "overdue_days" in data:
            cfg["overdue_days"] = max(1, min(365, int(data["overdue_days"])))
        for k in ("skip_rebuildable", "include_secrets", "mirror", "precheck"):
            if k in data:
                cfg[k] = bool(data[k])
        save_config(cfg)
    backup_dir, logs_dir = dest_names(cfg) if cfg["source"] else ("", "")
    return jsonify({"ok": True, "config": cfg, "backup_dir": backup_dir, "logs_dir": logs_dir})


def folder_size(path, skip_rebuildable):
    skip = set(REBUILDABLE_DIRS) if skip_rebuildable else set()
    files = size = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        is_junction = getattr(e, "is_junction", lambda: False)()
                        if e.name not in skip and not is_junction:
                            stack.append(e.path)
                    else:
                        files += 1
                        size += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return files, size


@app.route("/api/browse", methods=["POST"])
def api_browse():
    start = (request.get_json(force=True) or {}).get("start") or load_config()["source"] or "C:\\"
    win = WINDOW["w"]
    if win is not None:
        import webview
        folder = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        picked = win.create_file_dialog(folder, directory=start)
        path = picked[0] if picked else ""
        return jsonify({"path": os.path.normpath(path) if path else ""})
    # running from source in a browser: the normal Windows folder picker still opens on this PC
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(parent=root, initialdir=start, title="Select the folder to back up")
    root.destroy()
    return jsonify({"path": os.path.normpath(path) if path else ""})


@app.route("/api/folder_info")
def api_folder_info():
    path = request.args.get("path", "").strip().rstrip("\\")
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "error": "Folder not found"})
    skip = request.args.get("skip", "1") == "1"
    files, size = folder_size(path, skip)
    return jsonify({"ok": True, "files": files, "bytes": size, "name": Path(path).name})


@app.route("/api/dest_info")
def api_dest_info():
    drive = request.args.get("drive", "").upper()[:2]
    if not drive or not os.path.isdir(drive + "\\"):
        return jsonify({"ok": False, "folders": []})
    root = drive + "\\"
    try:
        names = [e.name for e in os.scandir(root) if e.is_dir()]
    except OSError:
        names = []
    # a backup folder is one with a matching "<name>_logs" folder next to it
    lower = {n.lower() for n in names}
    folders = sorted(n for n in names if (n + "_logs").lower() in lower)
    return jsonify({"ok": True, "drive": drive, "folders": folders})


@app.route("/api/disks")
def api_disks():
    cfg = load_config()
    return jsonify({"disks": list_disks(cfg), "last_drive": cfg["last_drive"],
                    "source_drive": cfg["source"][:2].upper(),
                    "system_drive": os.environ.get("SystemDrive", "C:").upper()})


@app.route("/api/history")
def api_history():
    cfg = load_config()
    drive = request.args.get("drive", "").upper()
    disks = {d["drive"]: d for d in list_disks(cfg)} if cfg["source"] else {}
    if drive in disks:
        runs = read_history(cfg, drive)
        save_cache(drive, disks[drive]["label"], runs)
        return jsonify({"drive": drive, "live": True, "runs": runs})
    cache = load_cache()
    if drive in cache:
        c = cache[drive]
        return jsonify({"drive": drive, "live": False, "saved": c["saved"], "runs": c["runs"]})
    if cache:
        drive, c = max(cache.items(), key=lambda kv: kv[1]["saved"])
        return jsonify({"drive": drive, "live": False, "saved": c["saved"], "runs": c["runs"]})
    return jsonify({"drive": drive, "live": False, "runs": []})


@app.route("/api/run", methods=["POST"])
def api_run():
    cfg = load_config()
    data = request.get_json(force=True) or {}
    drive = str(data.get("drive", "")).upper()[:2]
    dry_run = bool(data.get("test"))
    if not cfg["source"] or not os.path.isdir(cfg["source"]):
        return jsonify({"ok": False, "error": "Set the source folder in Settings first."}), 400
    if drive not in {d["drive"] for d in list_disks(cfg)}:
        return jsonify({"ok": False, "error": f"Disk {drive} is not connected."}), 400
    with STATE_LOCK:
        if STATE.get("running"):
            return jsonify({"ok": False, "error": "A backup is already running."}), 409
        STATE.clear()
        STATE.update(running=True, phase="start", drive=drive, test=dry_run,
                     mirror=cfg["mirror"], started=time.time(), files_done=0, bytes_done=0,
                     files_total=None, bytes_total=None, recent=[], failed_files=[])
    PROC["stop"] = False
    cfg["last_drive"] = drive
    save_config(cfg)
    threading.Thread(target=run_worker, args=(cfg, drive, dry_run), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    PROC["stop"] = True
    p = PROC["p"]
    if p and p.poll() is None:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    with STATE_LOCK:
        s = dict(STATE)
    if s.get("running"):
        s["elapsed"] = round(time.time() - s["started"])
    return jsonify(s)


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def stop_backup():
    PROC["stop"] = True
    p = PROC["p"]
    if p and p.poll() is None:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)


def on_closing():
    if STATE.get("running"):
        ok = WINDOW["w"].create_confirmation_dialog(
            "Backup running", "A backup is still running.\n\nStop it and close F.O.C.U.S. Backup?\n"
            "Files already copied stay on the disk; the next run copies the rest.")
        if not ok:
            return False
        stop_backup()
    return True


def message_box(text, title="F.O.C.U.S. Backup"):
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def main():
    if FROZEN and sys.stdout is None:
        # the .exe has no console window; give the web server somewhere harmless to write
        sys.stdout = sys.stderr = open(os.devnull, "w")
    cfg = load_config()
    port = free_port(int(os.environ.get("PORT", cfg["port"])))
    url = f"http://127.0.0.1:{port}"
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if os.environ.get("FOCUS_BROWSER") == "1":
        # developer option: show the dashboard in the normal browser instead of the app window
        print(f"F.O.C.U.S. Backup v{VERSION}: {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_backup()
        return

    import webview
    win = webview.create_window(f"F.O.C.U.S.© Backup v{VERSION}", url, width=1300, height=880,
                                min_size=(900, 620), background_color="#0d1b2e")
    WINDOW["w"] = win
    win.events.closing += on_closing
    try:
        webview.start(gui="edgechromium")
    except Exception:
        message_box("F.O.C.U.S. Backup needs Microsoft Edge WebView2, which is missing on this PC.\n\n"
                    "It is free from Microsoft. Click OK to open the download page, install the "
                    "\"Evergreen Bootstrapper\", then start F.O.C.U.S. Backup again.")
        webbrowser.open(WEBVIEW2_URL)
    stop_backup()
    server.shutdown()


if __name__ == "__main__":
    main()
