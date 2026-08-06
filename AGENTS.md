# Repository Guidelines

## Project Structure & Module Organization

`app/` contains configuration, SQLite access, market calendars, runtime helpers, remote data sources, server code, build orchestration, and valuation logic. `public/` contains the browser UI, while `config/fund_rules.json` is the source of truth for fund-specific holdings, proxies, and valuation rules. Root entry points build data (`build.py`), import one fund (`import_fund.py`), refresh announcements, launch the local server, and start the desktop wrapper.

Tests live in `tests/` and use Python's `unittest` framework. Operational helpers live in `scripts/`, PyInstaller configuration lives in `packaging/`, documentation lives in `docs/` and `0基础使用教程/`, and private-to-public release tooling lives in `private_public_sync/`. Treat `.venv/`, `data/`, local databases, logs, caches, `build/`, and `dist/` as generated or machine-local artifacts. `experimental/` and `private_public_sync/` are private-repository-only content and must not be copied into the public repository.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt`: install runtime dependencies.
- `python scripts/validate_config.py`: validate `config/fund_rules.json`.
- `python -m unittest discover -s tests -p 'test_*.py'`: run the offline test suite.
- `python -m compileall -q app scripts *.py`: check Python syntax.
- `node --check public/app.js`: check frontend JavaScript syntax when Node.js is available.
- `python build.py --current-only`: refresh only the data required for current valuations.
- `python import_fund.py <fund_code> --current-only`: validate one changed fund rule with a bounded refresh.
- `python serve.py`: start the local web application for API and UI checks.
- `./start.command` / `./stop.command`: start or stop the application on macOS.
- `.\quick_start.bat` / `.\stop.bat`: start or stop the application on Windows.
- `.\scripts\build_exe.ps1`: build the Windows executable when packaging changes are in scope.

Run the narrowest relevant tests first, then the complete suite. Avoid full historical builds, broad announcement refreshes, and live-data validation unless the change specifically requires them; these operations are slower, depend on external services, and may trigger rate limits.

## Coding Style & Testing Expectations

Use Python 3 with 4-space indentation, type hints on public interfaces, descriptive names, and small functions with explicit data-source and database boundaries. Keep frontend JavaScript and CSS compatible with the local HTTP server and existing API response shapes. Follow nearby code patterns instead of introducing a new framework or dependency without a clear need.

Add or update focused `unittest` coverage for behavior changes. Mock remote market, announcement, and fund-data sources in automated tests. Valuation-related changes should consider market calendars, quote freshness, stale or missing prices, FX conversion, holdings/proxy weights, coverage thresholds, purchase limits, and outlier handling. Database changes should also cover readiness checks, cache invalidation, and startup behavior where applicable.

## Fund Rules & Financial Data Safety

Changes to `config/fund_rules.json` must be explainable fund by fund. Validate the file after editing and prefer a single-fund current-only import before any broad refresh. Do not hand-edit generated SQLite data to make a rule appear correct.

This project presents indicative estimates, not guaranteed or official NAV values. Preserve the user-facing disclaimer and do not describe calculated values as official prices. Prefer cached data, bounded requests, and explicit timeouts. Never place proxy credentials, service tokens, private URLs, personal data, or raw sensitive source material in code, configuration, documentation, logs, tests, or commits.

## Private-to-Public Sync

The private repository is the development source of truth. Do not directly patch the sibling `LOF_iNAV_public` repository as part of normal development. Review `private_public_sync/public_repo_workflow.md` and `private_public_sync/public_release_checklist.md` before publishing.

On Windows, preview synchronization with `.\private_public_sync\sync_public.ps1 -DryRun`, then run `.\private_public_sync\sync_public.ps1` for the checked sync. The workflow mirrors files and can delete public-repository files that are absent from the private source, so inspect both repositories' status first. Never allow `.git/`, `.venv/`, `data/`, caches, `experimental/`, or `private_public_sync/` to enter the public repository.

## Agent Workflow & Logging

After completing a meaningful, independently verifiable phase, update the root `IMPLEMENTATION_LOG.md` (create it if absent) with the phase summary, verification performed, and exact local timestamp, for example `2026-07-11 16:30 Asia/Shanghai`. Then create a focused git commit without waiting for a separate prompt. Run `git status --short` first and stage only phase-related files. Preserve unrelated user edits and never include local databases, fetched data, `build/`, `dist/`, caches, or generated executable output. If verification is skipped or blocked, record the reason in the log before committing.

Every phase entry must also include a `Device` field containing the automatically detected hostname, operating system, and CPU architecture, for example `Device: WUTH-X (Windows 11, AMD64)` or `Device: MacBook-Pro (macOS, arm64)`. Detect these values from the local environment rather than guessing. Do not record usernames, serial numbers, MAC addresses, IP addresses, or other sensitive device identifiers.

## Commit & Change Hygiene

Keep changes focused and preserve unrelated user edits. Check `git status --short` before and after work, and never stage generated data, databases, logs, caches, build output, or executable artifacts. Use short imperative commit messages consistent with repository history. Automatically create the focused local phase commits required above, but do not push, synchronize the public repository, or create releases unless the user explicitly requests it.

For pull requests or handoff notes, describe valuation and data-source impact, list the exact verification commands run, call out any skipped network-backed checks, explain fund-rule changes individually, and include screenshots for visible UI changes.
