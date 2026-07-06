import logging
import os
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

from app.build import build_all
from app.config import DB_PATH
from app.server import URL, main


def working_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def open_browser() -> None:
    webbrowser.open(URL)


def database_has_funds() -> bool:
    path = Path(DB_PATH)
    if not path.exists():
        return False
    try:
        with sqlite3.connect(path) as con:
            row = con.execute("select count(*) from funds").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def ensure_initial_data() -> None:
    if database_has_funds():
        return

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
        print("The server will still start and retry lightweight refreshes from the web page.")
        return
    finally:
        root_logger.removeHandler(build_log_handler)
        root_logger.setLevel(previous_level)
    print(
        "Initial data build completed: "
        f"imported={len(result['imported'])} failed={len(result['import_failed'])}"
    )


if __name__ == "__main__":
    os.chdir(working_dir())
    ensure_initial_data()
    if os.environ.get("LOF_INAV_NO_BROWSER") != "1":
        threading.Timer(1.8, open_browser).start()
    main()
