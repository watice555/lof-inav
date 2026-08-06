from __future__ import annotations

import argparse
import logging

from app.build import build_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build local LOF iNAV data cache.")
    parser.add_argument("--days", type=int, default=30, help="backtest NAV rows to keep")
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="only fetch data needed for current valuation; skip daily prices and backtests",
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        raise SystemExit("--days must be at least 1")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    result = build_all(days=args.days, update_backtests=not args.current_only)
    import_failed = result["import_failed"]
    backtests_failed = result["backtests_failed"]
    if args.current_only:
        print(
            "build completed: current valuation data only; "
            f"imported={len(result['imported'])} failed={len(import_failed)}"
        )
    else:
        print(
            f"build completed: backtest days={args.days}; "
            f"imported={len(result['imported'])} import_failed={len(import_failed)} "
            f"backtests={len(result['backtests_refreshed'])} backtest_failed={len(backtests_failed)}"
        )
    for item in import_failed[:20]:
        print(f"import failed: {item['code']} {item['error']}")
    for item in backtests_failed[:20]:
        print(f"backtest failed: {item['code']} {item['error']}")
    if len(import_failed) > 20 or len(backtests_failed) > 20:
        print("additional failures were recorded in metadata:last_build_errors")
    return 1 if import_failed or (not args.current_only and backtests_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
