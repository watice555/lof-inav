from app.config import FUNDS
from app.db import connect, init_db
from app.sources import fetch_latest_regular_report, utc_now


def main() -> None:
    init_db()
    with connect() as con:
        for code in FUNDS:
            latest_report = fetch_latest_regular_report(code)
            if not latest_report:
                continue
            con.execute(
                """
                insert or replace into fund_announcements
                (fund_code, title, publish_date, announcement_id, url, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    latest_report["title"],
                    latest_report["publish_date"],
                    latest_report["announcement_id"],
                    latest_report["url"],
                    utc_now(),
                ),
            )
    print("announcements refreshed")


if __name__ == "__main__":
    main()
