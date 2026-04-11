from __future__ import annotations

import argparse
import shlex
import subprocess
import time


def _run(command: str) -> None:
    subprocess.run(command, shell=True, capture_output=True, text=True, check=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--message", default="Voici la capture d ecran")
    args = parser.parse_args()

    _run(f"xclip -selection clipboard -t image/png -i {shlex.quote(args.file)} || xclip -selection clipboard -i {shlex.quote(args.file)}")
    time.sleep(1)
    _run("wmctrl -a Discord || wmctrl -a discord || xdotool windowactivate $(xdotool search --name discord | head -n 1)")
    time.sleep(1)
    _run("xdotool type " + shlex.quote(args.message))
    time.sleep(0.5)
    _run("xdotool key control+v")
    time.sleep(1)
    _run("xdotool key Return")


if __name__ == "__main__":
    main()
