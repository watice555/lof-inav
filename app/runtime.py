from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def portable_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return resource_root()


def data_dir() -> Path:
    return Path(os.environ.get("LOF_INAV_DATA_DIR", str(portable_root() / "data")))


def db_path() -> Path:
    return Path(os.environ.get("LOF_INAV_DB_PATH", str(data_dir() / "lof_inav.sqlite3")))


def fund_rules_path() -> Path:
    configured = os.environ.get("LOF_INAV_FUND_RULES_PATH")
    if configured:
        return Path(configured)

    external_path = portable_root() / "config" / "fund_rules.json"
    if getattr(sys, "frozen", False) and external_path.exists():
        return external_path

    return resource_root() / "config" / "fund_rules.json"
