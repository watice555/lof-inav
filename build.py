from __future__ import annotations

import argparse

from app.build import build_all


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build local LOF iNAV data cache.")
    parser.add_argument("--days", type=int, default=30, help="backtest NAV rows to keep")
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="only fetch data needed for current valuation; skip daily prices and backtests",
    )
    args = parser.parse_args()
    if args.days < 1:
        raise SystemExit("--days must be at least 1")

    build_all(days=args.days, update_backtests=not args.current_only)
    if args.current_only:
        print("build completed: current valuation data only")
    else:
        print(f"build completed: backtest days={args.days}")
