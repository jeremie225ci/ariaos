from __future__ import annotations

import signal
import time

from .services.core import RuntimeStore

_RUNNING = True


def _stop(*_args) -> None:
    global _RUNNING
    _RUNNING = False


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    RuntimeStore.ensure_local_link_server()
    while _RUNNING:
        time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
