from __future__ import annotations

import argparse
import shutil
import subprocess
import time


def _launch_browser(url: str) -> bool:
    candidates = [
        ["google-chrome", "--password-store=basic", "--no-first-run", "--no-default-browser-check", "--new-window", url],
        ["chromium-browser", "--password-store=basic", "--no-first-run", "--no-default-browser-check", "--new-window", url],
        ["chromium", "--password-store=basic", "--no-first-run", "--no-default-browser-check", "--new-window", url],
        ["firefox", "--new-window", url],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except Exception:
                continue
    return False


def _run_shell(command: str) -> None:
    subprocess.run(command, shell=True, capture_output=True, text=True, check=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="about:blank")
    args = parser.parse_args()
    if not _launch_browser(args.url):
        _run_shell(f"xdg-open {args.url}")
    time.sleep(3)
    _run_shell("wmctrl -a chrome || wmctrl -a chromium || wmctrl -a firefox")
    time.sleep(1)


if __name__ == "__main__":
    main()
