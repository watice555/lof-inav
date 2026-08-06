import json

from app.build import refresh_reports
from app.db import connect, init_db


def main() -> None:
    init_db()
    with connect() as con:
        result = refresh_reports(con)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
