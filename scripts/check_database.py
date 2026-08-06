from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect, database_readiness, init_db


def main() -> int:
    init_db()
    with connect() as con:
        status = database_readiness(con)
    if status["ready"]:
        return 0
    print(json.dumps(status, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
