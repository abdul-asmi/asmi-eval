#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
UI_SCRIPT = os.path.join(ROOT, "ui.py")
POLL_SECONDS = 1.0


def get_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def start_server():
    print(f"Starting UI server: {UI_SCRIPT}")
    return subprocess.Popen([sys.executable, UI_SCRIPT], stdin=subprocess.DEVNULL, cwd=ROOT)


def stop_server(proc):
    if proc is None:
        return
    print(f"Stopping UI server (PID {proc.pid})")
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        print("UI server did not exit, killing")
        proc.kill()
        proc.wait(timeout=3)


def main():
    if not os.path.exists(UI_SCRIPT):
        print(f"Error: cannot find {UI_SCRIPT}")
        sys.exit(1)

    last_mtime = get_mtime(UI_SCRIPT)
    server = start_server()

    def handle_signal(signum, frame):
        print("Stopping watch mode")
        stop_server(server)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while True:
        time.sleep(POLL_SECONDS)
        current_mtime = get_mtime(UI_SCRIPT)
        if current_mtime != last_mtime:
            print(f"Change detected in {UI_SCRIPT}, restarting server...")
            last_mtime = current_mtime
            stop_server(server)
            server = start_server()
        elif server.poll() is not None:
            print(f"UI server exited unexpectedly with code {server.returncode}, restarting...")
            server = start_server()


if __name__ == '__main__':
    main()
