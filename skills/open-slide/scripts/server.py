"""open-slide dev server lifecycle: start / stop / status / open."""
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = SKILL_DIR / "runtime"
BOOTSTRAP = Path(__file__).resolve().parent / "bootstrap.py"
PID_FILE = Path(__file__).resolve().parent / ".server.pid"
PORT = 5173


def _port_open():
    for family in (socket.AF_INET6, socket.AF_INET):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                addr = ("::1", PORT) if family == socket.AF_INET6 else ("127.0.0.1", PORT)
                if s.connect_ex(addr) == 0:
                    return True
        except OSError:
            continue
    return False


def _ensure_runtime(slides_dir=None):
    cmd = [sys.executable, str(BOOTSTRAP)]
    if slides_dir:
        cmd += ["--slides-dir", slides_dir]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return json.loads(r.stdout) if r.stdout.strip() else {"status": "error", "message": r.stderr}
    return json.loads(r.stdout)


def start(slide_id=None, slides_dir=None):
    bootstrap_result = _ensure_runtime(slides_dir)
    if bootstrap_result.get("status") == "error":
        return bootstrap_result

    if _port_open():
        url = f"http://localhost:{PORT}"
        if slide_id:
            url += f"/s/{slide_id}"
            webbrowser.open(url)
        return {"status": "already_running", "url": url}

    npm = "npm.cmd" if os.name == "nt" else "npm"
    proc = subprocess.Popen(
        [npm, "run", "dev"],
        cwd=str(RUNTIME_DIR),
        shell=False,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    PID_FILE.write_text(str(proc.pid))

    for _ in range(30):
        time.sleep(0.5)
        if _port_open():
            break

    url = f"http://localhost:{PORT}"
    if slide_id:
        url += f"/s/{slide_id}"

    if _port_open():
        webbrowser.open(url)
        return {"status": "started", "pid": proc.pid, "url": url}
    return {"status": "timeout", "pid": proc.pid}


def stop():
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        subprocess.run(f"taskkill /PID {pid} /T /F", shell=True, capture_output=True)
        PID_FILE.unlink(missing_ok=True)
        return {"status": "stopped", "pid": pid}

    if _port_open():
        r = subprocess.run(
            f'netstat -ano | findstr ":{PORT}" | findstr "LISTENING"',
            shell=True, capture_output=True, text=True,
        )
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if parts:
                subprocess.run(f"taskkill /PID {parts[-1]} /T /F", shell=True, capture_output=True)
        return {"status": "stopped_by_port"}

    return {"status": "not_running"}


def status_cmd():
    pid = int(PID_FILE.read_text().strip()) if PID_FILE.exists() else None
    return {"running": _port_open(), "pid": pid, "port": PORT}


def open_browser(slide_id):
    url = f"http://localhost:{PORT}/s/{slide_id}"
    webbrowser.open(url)
    return {"url": url}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    slides_dir = None
    remaining = sys.argv[2:]
    positional = []
    i = 0
    while i < len(remaining):
        if remaining[i] == "--slides-dir" and i + 1 < len(remaining):
            slides_dir = remaining[i + 1]
            i += 2
        else:
            positional.append(remaining[i])
            i += 1

    if cmd == "start":
        result = start(positional[0] if positional else None, slides_dir)
    elif cmd == "stop":
        result = stop()
    elif cmd == "status":
        result = status_cmd()
    elif cmd == "open":
        result = open_browser(positional[0] if positional else "")
    else:
        result = {"error": f"unknown: {cmd}"}
    print(json.dumps(result, ensure_ascii=False))
