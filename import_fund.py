from __future__ import annotations

import argparse

from app.build import (
    import_fund_data,
    refresh_daily_prices,
    refresh_mark_prices,
    refresh_purchase_limits,
    refresh_quotes,
)
from app.config import FUNDS, FX_MIDPOINT_SECIDS
from app.db import connect, init_db, set_meta
from app.sources import utc_now
from app.valuation import backtest_summary, run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Import or refresh one configured LOF fund.")
    parser.add_argument("code", help="fund code, e.g. 160924")
    parser.add_argument("--days", type=int, default=30, help="backtest NAV rows to keep")
    parser.add_argument("--outliers", type=int, default=5, help="number of largest backtest errors to print")
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="only fetch data needed for current valuation; skip daily prices and backtest",
    )
    args = parser.parse_args()
    if args.days < 1:
        raise SystemExit("--days must be at least 1")

    if args.code not in FUNDS:
        configured = ", ".join(sorted(FUNDS))
        raise SystemExit(f"{args.code} is not configured. Configured funds: {configured}")

    init_db()
    with connect() as con:
        secids = import_fund_data(con, args.code)
        secids.update(FX_MIDPOINT_SECIDS.values())
        refresh_quotes(con, sorted(secids))
        refresh_purchase_limits(con)
        if not args.current_only:
            refresh_daily_prices(con, sorted(secids))
            refresh_mark_prices(con, sorted(secids))
            run_backtest(con, args.code, days=args.days)
        set_meta(con, f"last_import_fund_{args.code}_at", utc_now())
        set_meta(con, "last_navs_refresh_at", utc_now())
        set_meta(con, "last_navs_refresh_success_at", utc_now())

        fund = con.execute("select code, name, note from funds where code = ?", (args.code,)).fetchone()
        summary = None if args.current_only else backtest_summary(con, args.code)
        holdings = con.execute(
            """
            select report_date, secid, name, weight, source
            from holdings
            where fund_code = ?
              and report_date = (select max(report_date) from holdings where fund_code = ?)
            order by weight desc
            """,
            (args.code, args.code),
        ).fetchall()
        outliers = []
        if not args.current_only:
            outliers = con.execute(
                """
                select date, previous_date, actual_nav, estimated_nav, error_pct, covered_weight
                from backtests
                where fund_code = ?
                order by abs(error_pct) desc
                limit ?
                """,
                (args.code, args.outliers),
            ).fetchall()

    print(f"{fund['code']} {fund['name']}")
    print(f"note: {fund['note']}")
    print(f"backtest: {'skipped (--current-only)' if args.current_only else summary}")
    print("latest holdings:")
    for row in holdings:
        print(
            f"  {row['report_date']} {row['secid']} {row['name']} "
            f"weight={row['weight']:.4%} source={row['source']}"
        )
    if not args.current_only:
        print("largest errors:")
        for row in outliers:
            print(
                f"  {row['date']} prev={row['previous_date']} "
                f"actual={row['actual_nav']:.4f} estimated={row['estimated_nav']:.4f} "
                f"error={row['error_pct']:.4%} covered={row['covered_weight']:.2%}"
            )


if __name__ == "__main__":
    main()
