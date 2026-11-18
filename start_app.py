"""Safe Windows launcher for the XML/Excel Streamlit converter.

Run via run.bat (visible persistent command prompt), or:
    python start_app.py [--diagnose] [--no-install] [--no-browser]

Requires Python 3.10+. Installs dependencies only when missing. Does not
modify source workbooks, and writes diagnostic logs without workbook data.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
APPLICATION = ROOT / "xml_processor.py"
REQUIRED_MODULES = ("streamlit", "openpyxl")


class Reporter:
    def __init__(self):
        log_dir = ROOT / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = Path(tempfile.gettempdir()) / "XMLExcelPublicConverterLogs"
            log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"startup_{datetime.now():%Y%m%d_%H%M%S_%f}.log"
        self.file = self.path.open("a", encoding="utf-8", errors="replace")
        self.lock = threading.Lock()
        self.write(f"[INFO] Log file: {self.path}")

    def write(self, message: str, *, display: bool = True):
        with self.lock:
            self.file.write(message.rstrip("\n") + "\n")
            self.file.flush()
            if display:
                try:
                    print(message, flush=True)
                except UnicodeError:
                    print(message.encode("ascii", errors="replace").decode(), flush=True)

    def close(self):
        self.file.close()


def missing_modules() -> list[str]:
    missing = []
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    return missing


def install_dependencies(log: Reporter) -> bool:
    if not REQUIREMENTS.exists():
        log.write(f"[ERROR] Missing {REQUIREMENTS.name}; cannot prepare dependencies.")
        return False
    wheel_dir = ROOT / "wheels"
    wheel_files = list(wheel_dir.glob("*.whl")) if wheel_dir.is_dir() else []
    modes = [("offline wheels", True)] if wheel_files else []
    modes.append(("package index", False))
    for name, offline in modes:
        command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                   "--no-input", "--no-cache-dir", "--timeout", "8", "--retries", "1"]
        if offline:
            command += ["--no-index", "--find-links", str(wheel_dir)]
        if not (sys.prefix != sys.base_prefix):
            command += ["--user"]
        command += ["-r", str(REQUIREMENTS)]
        log.write(f"[INFO] Dependencies missing; attempting install from {name} (user scope, no admin).")
        try:
            # Stream to log and console so failures are never silent. Timeout is
            # bounded even when corporate network/proxy blocks the package index.
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", timeout=150)
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                raw = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout
                log.write(raw, display=False)
            log.write("[ERROR] Dependency install timed out. Check network/proxy or offline wheels.")
            continue
        except OSError as exc:
            log.write(f"[ERROR] Could not run pip: {exc}")
            continue
        log.write(result.stdout.rstrip(), display=False)
        if result.returncode == 0 and not missing_modules():
            log.write("[OK] Required packages are available.")
            return True
        log.write(f"[WARN] Install attempt failed (exit={result.returncode}).")
        if result.stdout:
            for line in result.stdout.splitlines()[-12:]:
                log.write("  " + line)
    log.write("[ERROR] Cannot install required Python packages.")
    log.write("[TIP] On an internet-connected computer with the same Python version, run:")
    log.write("      python -m pip download -r requirements.txt -d wheels")
    log.write("      Copy the wheels folder into this project and run run.bat again.")
    return False


def choose_free_port(start: int = 8501, stop: int = 8511) -> int:
    for port in range(start, stop):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("All localhost ports 8501-8510 are unavailable.")


def is_server_ready(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/_stcore/health"
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def open_local_browser(url: str, log: Reporter):
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        elif not webbrowser.open(url):
            log.write("[WARN] Could not auto-open your browser; paste URL manually.")
            return
        log.write("[INFO] Asked the default browser to open the local app.")
    except Exception as exc:
        log.write(f"[WARN] Browser could not be started automatically: {exc}")
        log.write("[TIP] Copy the URL above into Chrome or Edge.")


def serve(log: Reporter, *, browser: bool = True) -> int:
    try:
        port = choose_free_port()
    except RuntimeError as exc:
        log.write(f"[ERROR] {exc}")
        return 1
    url = f"http://127.0.0.1:{port}"
    command = [sys.executable, "-u", "-m", "streamlit", "run", str(APPLICATION),
               "--server.address", "127.0.0.1", "--server.port", str(port),
               "--server.headless", "true", "--browser.gatherUsageStats", "false",
               "--server.fileWatcherType", "none"]
    log.write("[INFO] Starting Streamlit on the local PC; no cloud publishing.")
    try:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", bufsize=1)
    except OSError as exc:
        log.write(f"[ERROR] Cannot start Streamlit: {exc}")
        return 1
    output_tail: deque[str] = deque(maxlen=40)

    def pump_output():
        assert process.stdout is not None
        for line in process.stdout:
            output_tail.append(line.rstrip())
            log.write(line.rstrip())

    reader = threading.Thread(target=pump_output, daemon=True)
    reader.start()
    try:
        for _ in range(100):
            if process.poll() is not None:
                log.write(f"[ERROR] Streamlit exited before startup. Code: {process.returncode}")
                return process.returncode or 1
            if is_server_ready(port):
                log.write("[OK] Web UI is running.")
                log.write(f"[URL] {url}")
                log.write("[INFO] Keep this window open while using the application.")
                log.write("[INFO] Press Ctrl+C to stop the app.")
                if browser:
                    open_local_browser(url, log)
                return process.wait()
            time.sleep(0.5)
        log.write("[ERROR] Streamlit did not become ready. Check the log and Windows firewall.")
        return 1
    except KeyboardInterrupt:
        log.write("[INFO] Shutdown requested (Ctrl+C).")
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows-safe Streamlit project launcher")
    parser.add_argument("--diagnose", action="store_true", help="Do not install or run app; print readiness")
    parser.add_argument("--no-install", action="store_true", help="Do not automatically install dependencies")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the default browser")
    args = parser.parse_args(argv)
    reporter = Reporter()
    try:
        reporter.write(f"[INFO] Python: {sys.executable} ({sys.version.split()[0]})")
        reporter.write(f"[INFO] Project: {ROOT}")
        if sys.version_info < (3, 10):
            reporter.write("[ERROR] Python 3.10 or later is required.")
            return 2
        if not APPLICATION.is_file():
            reporter.write(f"[ERROR] Application file missing: {APPLICATION.name}")
            return 2
        missing = missing_modules()
        if missing:
            reporter.write("[WARN] Missing or broken Python packages: " + ", ".join(missing))
            if args.diagnose or args.no_install:
                reporter.write("[TIP] Run run.bat to prepare packages, or use the offline wheels folder.")
                return 3
            if not install_dependencies(reporter):
                return 3
        else:
            reporter.write("[OK] Python packages streamlit and openpyxl import successfully.")
        if args.diagnose:
            reporter.write("[OK] Diagnostic completed (no app launched).")
            return 0
        return serve(reporter, browser=not args.no_browser)
    except Exception as exc:
        reporter.write(f"[ERROR] Unexpected startup error: {type(exc).__name__}: {exc}")
        return 4
    finally:
        reporter.write(f"[INFO] Startup log saved: {reporter.path}")
        reporter.close()


if __name__ == "__main__":
    raise SystemExit(main())
