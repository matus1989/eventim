"""Uruchomienie przez ``python -m eventim_watcher``."""

from __future__ import annotations

import sys

from eventim_watcher.main import main

if __name__ == "__main__":
    sys.exit(main())