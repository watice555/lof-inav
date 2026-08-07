from __future__ import annotations

import os
import shutil
import sys
import tempfile
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


def seed_db_path() -> Path:
    return resource_root() / "seed" / "lof_inav.sqlite3"


def install_seed_database(
    seed_path: Path | None = None,
    target_path: Path | None = None,
) -> bool:
    """Install the packaged seed once without overwriting an existing cache."""
    source = seed_path or seed_db_path()
    target = target_path or db_path()
    if target.exists() or not source.is_file():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with source.open("rb") as seed_file:
                shutil.copyfileobj(seed_file, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        # The desktop entry point is single-process. Recheck before the atomic
        # replace so a pre-existing user database always wins.
        if target.exists():
            return False
        os.replace(temporary_path, target)
        temporary_path = None
        return True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fund_rules_path() -> Path:
    configured = os.environ.get("LOF_INAV_FUND_RULES_PATH")
    if configured:
        return Path(configured)

    external_path = portable_root() / "config" / "fund_rules.json"
    if getattr(sys, "frozen", False) and external_path.exists():
        return external_path

    return resource_root() / "config" / "fund_rules.json"
