import logging
import os
import sys
from pathlib import Path

from app.build import build_all
from app.db import connect, database_is_ready, init_db
from app.server import main


def working_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def database_ready() -> bool:
    try:
        init_db()
        with connect() as con:
            return database_is_ready(con)
    except Exception:
        return False


def ensure_initial_data() -> bool:
    if database_ready():
        return True

    root_logger = logging.getLogger()
    previous_level = root_logger.level
    build_log_handler = logging.StreamHandler()
    build_log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(build_log_handler)
    print("First run: building current valuation data. This may take a few minutes...")
    try:
        result = build_all(update_backtests=False)
    except Exception as exc:
        print(f"Initial data build failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        root_logger.removeHandler(build_log_handler)
        root_logger.setLevel(previous_level)
    print(
        "Initial data build completed: "
        f"imported={len(result['imported'])} failed={len(result['import_failed'])}"
    )
    if result["import_failed"]:
        return False
    return database_ready()


if __name__ == "__main__":
    os.chdir(working_dir())
    if not ensure_initial_data():
        raise SystemExit(
            "Initial data is incomplete. Check the errors above and run the application again."
        )
    os.environ["LOF_INAV_OPEN_BROWSER"] = "1"
    main()
