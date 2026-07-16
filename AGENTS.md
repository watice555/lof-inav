# Repository Guidelines

## Project Structure & Module Organization

`app/` contains configuration, database, market calendar, runtime, source, server, and valuation logic. `public/` contains the browser UI, `config/fund_rules.json` contains fund rules, `scripts/` contains validation and packaging helpers, and `packaging/` contains the PyInstaller specification. Root entry points build data (`build.py`), import individual funds (`import_fund.py`), refresh announcements, launch the server, and start the desktop wrapper. Documentation lives in `docs/` and `0基础使用教程/`. Treat `build/`, `dist/`, local databases, caches, and fetched market data as generated artifacts.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt`: install runtime dependencies.
- `python scripts\validate_config.py`: validate fund rules and configuration.
- `python -m compileall -q app scripts *.py`: check Python syntax.
- `python build.py --current-only`: perform the smaller current-data build when network-backed validation is required.
- `python serve.py`: start the local web application for manual API/UI checks.
- `.\quick_start.bat`: create the environment, build current data if necessary, and launch the app; use only for an end-to-end check.
- `.\scripts\build_exe.ps1`: build the Windows executable when packaging changes are in scope.

Avoid full historical builds and live data refreshes for routine code validation. There is currently no dedicated `tests/` suite, so add focused offline tests when extracting testable business logic.

## Coding Style & Testing

Use Python 3 with 4-space indentation, type hints on public interfaces, and small functions with clear units and data-source boundaries. Keep frontend JavaScript and CSS changes compatible with the local server's API. Valuation changes should cover market calendars, stale or missing quotes, FX conversion, holdings/proxy rules, and outliers. Mock remote market and announcement sources in automated checks.

## Commit & Pull Request Guidelines

Keep commits focused and use short imperative messages. Pull requests should describe valuation or data-source impact, list validation commands, and include screenshots for UI changes. Explain changes to `config/fund_rules.json` fund by fund.

## Agent Workflow & Logging

After completing a meaningful, independently verifiable phase, update the root `IMPLEMENTATION_LOG.md` (create it if absent) with the phase summary, verification performed, and exact local timestamp, for example `2026-07-11 16:30 Asia/Shanghai`. Then create a focused git commit without waiting for a separate prompt. Run `git status --short` first and stage only phase-related files. Preserve unrelated user edits and never include local databases, fetched data, `build/`, `dist/`, caches, or generated executable output. If verification is skipped or blocked, record the reason in the log before committing.

Every phase entry must also include a `Device` field containing the automatically detected hostname, operating system, and CPU architecture, for example `Device: WUTH-X (Windows 11, AMD64)` or `Device: MacBook-Pro (macOS, arm64)`. Detect these values from the local environment rather than guessing. Do not record usernames, serial numbers, MAC addresses, IP addresses, or other sensitive device identifiers.

## Financial Data & Network Safety

This project presents indicative estimates, not guaranteed NAV values. Preserve the user-facing disclaimer and do not describe estimates as official prices. Network-backed commands can be slow and can trigger rate limits; prefer cached data, bounded requests, and explicit timeouts. Never place private proxy credentials or service tokens in rules, docs, logs, or commits.
