# Implementation Log

## 2026-11-15 01:33:27 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Prepared the public source and Windows x64 Portable package for `v1.1.0`. The release includes bid/ask-based smart premiums, synchronized page and scheduled report filters, current-data refresh changes, backtest cache recovery, seven third-quarter fund rule updates, and the retirement of 165512 from active refresh. Preserved prior public release entries while keeping local-only operational entries in the development log.
- Verification: Previewed the mirror and checked the public source paths, file contents and permission modes against the development tree, with the public release log maintained separately. Passed all 218 macOS offline tests with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, configuration validation, Python compilation, `node --check public/app.js`, PowerShell parsing, dependency consistency, `git diff --check`, and checks for generated files and credentials. A headless browser checked both threshold filters, CSV/PNG, and the mobile smart premium column against 337 funds from a temporary copy of the seed. Windows x64 built the package with the existing PowerShell 5.1 script, ran the offline suite with the macOS-only module skipped, and passed extracted EXE startup, seeded database, fund page and API, the bundled PowerShell shutdown helper, and SQLite `quick_check`. The final ZIP uses ZIP-standard separators with the same packaged file contents and passed SHA-256 and archive integrity checks. No live provider refresh, Telegram delivery, or Windows Defender scan was run for this release.

## 2026-11-15 01:27:07 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Marked the macOS LaunchAgent test module as platform-specific before it imports macOS dependencies. The initial Windows packaging run stopped during offline test discovery because Windows does not provide `pwd`.
- Verification: All four LaunchAgent tests and all 218 offline tests passed on macOS, along with `git diff --check`. The Windows packaging run will verify that the macOS module is skipped and the other tests execute normally. No application or provider behavior changed.

## 2026-11-15 01:12:39 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Brought the Windows Portable fixes from the public v1.0.0 release back into the development source before mirroring. Restored PowerShell 5.1-compatible packaging names and HTTP logging in windowed PyInstaller builds, with the existing public regression tests.
- Verification: Passed the 24 focused server startup and Portable tests, all 218 offline tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing, and `git diff --check`. Windows executable acceptance is deferred to the release build. No provider refresh was required.

## 2026-11-15 00:30:47 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Restored the macOS Telegram smart premium report to 14:30 on weekdays and updated the installer and documentation.
- Verification: Passed all four LaunchAgent tests and all 216 offline tests. Reinstalled the local LaunchAgent, validated the plist, and verified all five loaded 14:30 triggers with launchctl. Skipped manual report delivery because only the trigger time changed.

## 2026-11-14 22:10:53 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Aligned the browser’s sell-premium and buy-discount filters with the scheduled Telegram report. Both require a valid smart premium signal over the applicable threshold; the report defaults to 1.5%, lists the bid for sell premiums and the ask for buy discounts, and retains the paused-purchase and failed-NAV exclusions. Moved the installed macOS LaunchAgent from 14:30 to 14:40 on weekdays, with the existing China trading-day check. No fund rules, valuation formulas or data sources changed.
- Verification: Passed all 23 targeted alert tests and all 216 offline tests via `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, plus `.venv/bin/python -m compileall -q app scripts *.py`, `.venv/bin/python scripts/validate_config.py`, `node --check public/app.js`, and `git diff --check`. Headless Chromium checked both controls, threshold boundaries, invalid and missing signals, the all-results view and CSV export on desktop and mobile. The browser and a dry-run CLI report read the same local snapshot of 337 funds and found 7 sell premiums and 5 buy discounts with paused purchases excluded. Inspected the screenshot and verified all five 14:40 triggers in the installed plist and loaded launchd job. The CLI preview mocked the forced refresh; no provider refresh or Telegram test message was needed for these checks.

## 2026-11-14 21:26:16 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Restored live cache refresh and smart premium display. The Refresh button requests NAV and quote updates, displays the latest completed refresh time, and shows fresh premiums as soon as quotes finish. A visible idle tab checks every five minutes, within the 15-minute quote lifetime. Read-only snapshots cannot populate the automatic-refresh cache, and requests that overlap invalidation cannot repopulate it with old data. The local server reuses the existing instance for its data directory. Same-session Sina closing books are accepted when their closing price matches Eastmoney's later-timestamped close. Added verified Yahoo fallbacks for VNET (164906) and IMVP (164824).
- Data: Consolidated three verified project server processes into one at port 8001. Backfilled 18 November 13 index closes using the official CSI intraday closing points, then recomputed the 22 affected funds after the 337-fund incremental backtest. Verified 858 current valuation base-price targets with zero gaps. The November 14 refresh saved 1,168 of 1,168 quotes at 21:21:48 and checked all 337 NAVs without failures at 21:22:38. All 337 funds have November 13 order books and smart premium results (134 buy discounts, 90 sell premiums, 113 no opportunity). The 28 funds dated November 12 were separately confirmed against Eastmoney NAV history, with 309 dated November 13. Backtests match all 337 latest NAV dates with zero dirty funds. Thirteen fund daily close prices remain unavailable on their latest NAV dates, with corresponding gaps in historical trade premiums.
- Verification: Passed focused startup, order-book and index-source tests and all 214 tests via `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`. Passed `.venv/bin/python -m compileall -q app scripts *.py`, `.venv/bin/python scripts/validate_config.py`, `.venv/bin/python scripts/check_database.py`, `node --check public/app.js` and `git diff --check`. Verified database readiness with backtests required, duplicate launch reuse on the running server, and the local read-only funds and refresh-status APIs. Headless Chrome clicked Refresh, verified both tasks started, and confirmed the table loaded the new quote data while NAV refresh was still running. Inspected the captured desktop screenshot. Windows startup runtime was unavailable on this Mac. No full historical rebuild was needed.

## 2026-11-13 15:35:21 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Added a default-visible smart premium column that selects a buy discount from the ask or a sell premium from the bid, and displays no opportunity when the estimate falls within a complete valid book. Missing or stale inputs, incomplete books without a qualifying side, and crossed books receive separate states. Kept trade price, bid/ask, trade premium, buy discount and sell premium as optional columns hidden by default; the directional detail columns now show only their relevant sign. Migrated earlier column preferences to the new default visibility while preserving unrelated choices and honoring subsequent v5 customization. Switched browser sorting/filtering and CSV/PNG exports to the smart signal, retaining the underlying trade/directional API values and existing alert selection. Hover details preserve access to quote status, book quantities and timestamp. No fund rule, source request, database schema or backtest changes were needed.
- Verification: Passed 16 focused tests with `.venv/bin/python -m unittest discover -s tests -p 'test_order_book.py'` and all 210 offline tests with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, plus `.venv/bin/python -m compileall -q app scripts *.py`, `node --check public/app.js`, and `git diff --check`. Added coverage for positive/negative opportunities with and without trades, equality/no-opportunity boundaries, single-sided and crossed books, missing estimates/quotes, stale and failed snapshots, and compact API fields. Replayed the previously verified nine-fund November 12 snapshots in a temporary database and checked the actual API/UI: six default columns, all five optional columns available, smart sorting, two qualifying sellers and two qualifying buyers at a 0.1% threshold, CSV/PNG exports, reset-to-default, v2/v3/v4 migration and v5 customization. Browser checks at 1440, 1180 and 390 pixels found no smart-column overflow or page errors; mocked unavailable responses remained distinct from no opportunity. Inspected the default-view screenshot, stored outside the repository. Live provider requests and historical rebuilds were skipped because this phase changes calculation selection and presentation only; production data and Bot delivery were untouched.

## 2026-11-13 15:02:45 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Added separate indicative buy and sell premiums to the valuation API and fund table. Buy premium uses ask / current system estimate - 1; sell premium uses bid / current system estimate - 1, independent of the sign or whether any trades occurred. Each requires its own available book side and a finite positive estimate, and expired or failed snapshots yield empty values. Preserved the trade-based premium and explicitly labeled its existing filter. Added sortable default columns, formula and quantity/fee tooltips, CSV/PNG exports with matching colors, and column-preference migration that preserves prior choices while enabling the two new columns. Fund rules, data-source requests, historical backtests and alert selection remain unchanged.
- Verification: Passed 13 focused tests using `.venv/bin/python -m unittest discover -s tests -p 'test_order_book.py'` and all 207 offline tests using `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`. Passed `.venv/bin/python -m compileall -q app scripts *.py`, `node --check public/app.js`, and `git diff --check`. Tests cover trades and zero trades, both signs and zero premiums, one-sided or missing books, unavailable/non-positive/non-finite estimates, current-estimate denominator selection, stale/failing snapshots, and compact API serialization. Replayed the previously verified nine-fund November 12 snapshots through the normal refresh writer in a temporary database; checked actual API values and browser cells, ascending sorting on both columns, CSV/PNG downloads, v2/v3/v4 preference handling, and layouts at 1440, 1180 and 390 pixels with no page errors or book/header overflow. Inspected the table screenshot and exported PNG; screenshots and temporary reports remain outside the repository. New live-data requests and historical rebuilds were skipped because this phase changes calculations and presentation only; no production database write or Bot delivery occurred.

## 2026-11-13 04:53:42 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Added independent trade state, traded volume and bid/ask price and quantity fields to realtime quotes. Sina now supplies fund top-of-book snapshots in fund units even when Eastmoney has no usable trade, and zero volume with a valid timestamp and at least one valid book side is retained as no-trade data. No-trade snapshots clear prior trade prices and percentages; neither book prices nor previous-close placeholders enter trade premiums or asset returns. Failed or expired snapshots hide their cached book. The UI and exports show bid/ask, quantities and snapshot time, distinguish no trades from fetch failure, and preserve existing column preferences while enabling the new book column. Refresh status now aggregates per-instrument outcomes for active instruments; partial retries preserve outstanding failures. Existing databases receive an additive quote migration without rebuilding historical caches.
- Verification: Passed 10 focused tests with `.venv/bin/python -m unittest discover -s tests -p 'test_order_book.py'` and all 204 offline tests with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`. Passed `.venv/bin/python -m compileall -q app scripts *.py`, `node --check public/app.js`, and `git diff --check`. Coverage includes single-sided and malformed books, missing volume/time, fallback ordering, snapshot replacement and recovery, stale cache suppression, valuation exclusion, partial retries, the status API and idempotent migration preserving build readiness and backtests. A bounded normal refresh against a temporary SQLite backup recovered all nine reported funds from the actual sources with zero missing quotes; all retained November 12 no-trade snapshots with null trade prices and premiums. 160526 matched bid 1.199 / 4,500 units and ask 1.209 / 7,300 units. Headless Chromium checked the actual temporary API/UI, nine visible rows, the new column and old preference migration, with no page errors or desktop column overflow; captured desktop and mobile screenshots outside the repository. No full historical build, broad live refresh, production database write, or Bot delivery was performed.

## 2026-11-12 19:11:34 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Added the seven missing 2026Q3 holdings snapshots for reports dated 2026-09-30 and published 2026-10-27. 501301 uses its target ETF's benchmark at 94.74%; 164824 uses a seven-symbol India ETF basket at 89.02%, including the renamed IMVP; 160416 uses IXC at 93.68%; 160719 uses a 94.86% gold proxy; 160723 uses 56.97% WTI and 38.24% Brent; 164701 uses 99.61% gold equivalent, including the daily 2x target on the 6.29% UGL position. For 164906, the report's top ten sum to 57.32%; the remaining 35.07% uses the 2026-09-22 KraneShares KWEB UCITS equity basket for the same revised benchmark, excluding those ten and scaling the remainder by listing market to match the fund's 71.59% Hong Kong and 20.80% US disclosure. Prior report periods remain intact.
- Verification: Cross-checked extracted investment tables against 11 rendered pages from the seven original reports. Verified weights, regional totals, unique identifiers, dates, and the unchanged earlier holdings. Passed 19 focused snapshot/report tests and all 194 offline tests with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, plus configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Ran `import_fund.py 164906 --current-only` against a temporary SQLite database. Ran the normal report refresh on the seven target codes in the live database, confirming seven successful imports, 75 recomputed backtest rows, seven latest backtest dates matching NAV, and zero unresolved current base-price targets. Refreshed quotes through the local API and verified all 64 identifiers for these seven funds, 100% price coverage of modeled holdings, available premiums, and zero data alerts for the seven. No full historical build or Bot delivery was needed.

## 2026-11-12 19:08:02 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Archived 165512 (中信保诚新机遇混合 LOF) effective from its 2026-10-28 delisting. Active imports, the live list, and current quote collection now exclude the fund. Its existing valuation rule and historical rows are retained. Archiving alone does not change the valuation configuration hash or invalidate any backtests.
- Verification: Passed the focused configuration and database readiness tests and all 194 offline tests (`.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`), `.venv/bin/python scripts/validate_config.py`, `.venv/bin/python scripts/check_database.py --require-backtests`, `.venv/bin/python -m compileall -q app scripts *.py`, and `git diff --check`. Restarted the verified local service on port 8001 and confirmed 337 active funds, 165512 absent from active quote collection, and retained holdings and backtest detail API responses. Compared historical row counts and contents before and after restart: 3,688 NAV, 276 holdings, 852 backtest, and 600 daily-price rows for 165512 matched.
- Market check: Queried all 284 entries in the Shenzhen Stock Exchange LOF list and the 2026-11-12 Eastmoney, Sina, and Tencent snapshots for 160526, 161131, 161222, 161232, 161729, 161834, 165309, 167003, and 168104. All nine remain listed and showed bid and ask quotes on Sina but zero trades. Eastmoney did not publish a trade price, and Tencent showed the prior close with zero volume. The live fund list correctly leaves the trade price and premium empty for each. A full historical rebuild and a single-fund import were unnecessary because this phase changes refresh membership and keeps the existing history.

## 2026-10-15 15:19 Asia/Shanghai

Model: qwen3.8-flash-next (plan reviewed via Codex CLI gpt-6-astra, medium reasoning, read-only)

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Split the Telegram premium alert report into two market groups — 「非A股（重点）」 first, 「A股」 second — so non-A-share funds (QDII/商品/债券 etc.) stand out. Classification rule: `type` string starting with "A股" is A-share; everything else (including missing/invalid type) is non-A-share. Changes are confined to `app/premium_alerts.py`: added `is_a_share_fund()` and `append_market_group()`; `build_report()` now stably partitions the already-selected/sorted premium and discount lists and renders both groups with per-group counts. Report header adds a "命中：非A股 N 只（重点）；A股 M 只" summary line and a warning-count pointer, and the disclaimer moved to the header so truncation (3800-char Telegram budget) still keeps counts and disclaimer. `select_threshold_funds`, `append_fund_section`, `truncate_message`, `send_bot_message`, the CLI script and the macOS launch-agent schedule are unchanged; still one refresh, one report string, one send.
- Verification: Added 4 new tests plus extended assertions in `tests/test_premium_alerts.py` (group splitting/ordering, empty groups, `is_a_share_fund` classification incl. missing/None/empty/non-str types, grouped-report truncation keeping header). Passed the 3 focused files (`test_premium_alerts`, `test_alert_sorting`, `test_macos_premium_alert`, 22 tests) and the full offline suite (`unittest discover -s tests`, 192 tests, OK). One initial assertion failure (warning count 3 vs actual 4) was a test-side miscount and was fixed. No live refresh or real Telegram send was performed.

## 2026-10-08 15:37:23 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Added 52 explicit Yahoo symbol mappings for the US holdings missing from today's quote refresh, preserving their internal secids and fund weights. These mappings also enable the existing Yahoo daily-price path for those holdings. Verified Yahoo returned usable quotes for all 52, including Qnity Electronics (Q), before applying the configuration. Retried only the previous 71 missing quote instruments through the normal refresh routine, then refreshed missing current valuation base prices.
- Verification: Passed 11 focused source tests with `.venv/bin/python -m unittest discover -s tests -p 'test_index_price_sources.py'`, all 188 offline tests with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, `.venv/bin/python scripts/validate_config.py`, `.venv/bin/python -m compileall -q app scripts *.py`, and `git diff --check`. Mocked source tests check fallback routing, preserved identifiers, prices and timestamps for NASDAQ and NYSE holdings. Live refresh recovered 60/71 quotes at 15:36:36 Asia/Shanghai, including all 52 new mappings; 10 Shenzhen funds and HK 00501 remained missing. Current valuation base-price gaps fell from 28 to 2 (WDC and ZBRA) at 15:36:48. No broad historical build or bot resend was performed. Generated database contents were not staged.

## 2026-10-08 14:46:50 Asia/Shanghai

Model: GPT-6

Device: TianhaodeMacBook-Pro (macOS 27.0.1, arm64)

- Phase: Increased the premium alert's shared API/CLI default refresh timeout from 600 to 1800 seconds. Timeout notices now show actual elapsed time, the configured limit, each refresh's waiting/running/completed state, and available structured phase/count progress; failed refresh notices also include structured state without exposing raw provider messages. Updated the bot documentation. Verified the installed LaunchAgent invokes this checkout without a timeout override, so the next scheduled run picks up the change.
- Verification: Passed all 13 focused tests with `.venv/bin/python -m unittest discover -s tests -p 'test_premium_alerts.py'` and all 187 offline tests with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`; passed `.venv/bin/python -m compileall -q app scripts *.py` and `git diff --check`. Added mocked-clock coverage for the 30-minute default, custom timeout, blocked startup, day-price progress, completed quote timestamp fallback, and failure-message privacy. Live refresh and bot delivery were skipped to avoid unnecessary provider requests or sending an unsolicited message.

## 2026-09-02 15:37:12 Asia/Shanghai

Model: GPT-5

Device: TianhaodeMacBook-Pro.local (macOS 26.6.2, arm64)

- Phase: Made persisted backtest-cache upgrades self-healing. A version migration now deletes incompatible derived rows, queues every configured fund with NAV data for the normal bounded seven-day rebuild window, clears the misleading prior completion timestamp, and records durable rebuild state. After binding the local server, one process claims a shared SQLite lease and rebuilds asynchronously; sibling ports cannot duplicate the job, partial failures remain pending, and automatic retries observe a one-hour cooldown. Current-only builds still intentionally disable and discard rebuild state. The API and browser now distinguish upgrade rebuilding or waiting from ordinary missing backtests and suppress hundreds of per-fund missing-cache warnings during recovery.
- Verification: Passed all 183 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Exercised the migration against an in-memory clone of the current local database: version 6 advanced to 7, all 338 configured funds were queued, the old completion timestamp was cleared, and the source database remained unchanged at version 6 with zero backtest rows. Live network-backed rebuilding was intentionally deferred until the updated service is restarted, avoiding duplicate provider calls while the old processes remain active.

## 2026-08-30 06:06:53 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.6.2, arm64)

- Phase: Aligned incremental backtest price-target discovery with the backtest recomputation query when a legacy or migrated dirty anchor predates the first stored NAV. The target query now falls back to the fund's earliest NAV instead of returning an empty window, ensuring the full recomputation window requests its required holding, FX, and fund trade prices before replacing cached rows.
- Verification: Passed all 18 focused backtest-cache tests, including a new regression for an anchor before the NAV series; passed all 179 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Network-backed refreshes were skipped because this SQL boundary is covered deterministically with an in-memory database.

## 2026-08-30 05:27:12 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.6.2, arm64)

- Phase: Implemented the unified backtest-derived-cache invalidation protocol. Added backward-compatible dirty-state metadata with monotonic anchors and transactional invalidation/resolution; made historical NAV revisions and backfills plus valuation-relevant holding snapshot changes delete affected derived rows and mark the earliest dirty date; moved dirty clearing into the atomic full/incremental backtest replacement path; and reduced report/NAV refresh code to scheduling and consuming that shared state. Preserved dirty state for uncovered windows and future anchors, retained it across unchanged imports and failures, kept standalone NAV upserts caller-transactional, ignored removed fund codes in retry scheduling, migrated Portable seed metadata, and corrected the UI warning so it no longer claims that an invalidated stale cache is retained. Raised the backtest cache version once so protocol upgrades also remove unmarked stale rows left by older `--current-only` imports.
- Verification: Passed 67 focused dirty-state, backtest-cache, fund-snapshot, NAV/report refresh, coordination, and Portable seed tests; passed all 178 offline `unittest` tests; passed configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Added explicit coverage for legacy metadata migration, cache-version upgrade invalidation, full/incremental clearing bounds, caller rollback, atomic failure rollback, historical NAV insertion, every valuation-relevant holding signature field, older-report backfill, announcement-only changes, disabled backtests, and partial failures. Network-backed fund, report, and market-data refreshes were skipped because the state transitions and failure paths are deterministic and fully mocked in the offline suite.

## 2026-08-30 03:29:18 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.6.2, arm64)

- Phase: Converted the A2 review finding into an executable backtest-derived-cache invalidation protocol in `docs/backtest_invalidation_plan.md`. The plan defines a backward-compatible dirty-state metadata model, monotonic invalidation and atomic resolution invariants, NAV revision/backfill and semantic holding-snapshot detection, exact treatment of older report backfills, full versus incremental recomputation coverage, scheduler responsibilities, a cross-layer test matrix, and three parallel implementation workstreams.
- Verification: Cross-checked every planned boundary against the current `import_fund_data`, `upsert_nav_rows`, `refresh_reports`, `refresh_incremental_backtests`, `run_backtest`, `run_backtest_incremental`, and `replace_backtest_rows_atomic` implementations. Confirmed the worktree was clean before the documentation phase and ran `git diff --check` on the resulting plan and log update. No runtime source code changed in this phase, so the existing 155-test baseline remains the implementation starting point.

## 2026-08-30 00:18:46 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.6.2, arm64)

- Phase: Extended the China market calendar through 2027 using a preliminary closure schedule derived from the State Council's current holiday and adjustment rules. Marked the 2027 CN calendar as provisional pending the Shanghai Stock Exchange's annual notice, surfaced that status in both premium-alert runners, retained the conservative unknown-year behavior from 2028 onward, and documented the source and confirmation requirement. The existing Hong Kong calendar already covered 2027 and was left unchanged.
- Verification: Passed 19 focused market-calendar and premium-alert tests, all 155 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Network-backed fund and market-data refreshes were skipped because this calendar-only change is covered by deterministic offline checks and the official 2027 SSE closure notice has not yet been published.

## 2026-08-30 00:17 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.6.2, arm64)

- Phase: Completed a four-track read-only code review (server/security, data sources/network, build/SQLite, valuation/frontend) at commit `3071636`, cross-checked the findings against an external review, and published the calibrated results as `docs/project_review_2026-08.md`. The baseline report classifies findings into confirmed defects (A1-A6), design trade-offs requiring product decisions (B1-B4), performance hypotheses requiring benchmarks (C1-C4), and future risks (D1-D2), each with reproduction steps, impact, confidence, fix direction, and acceptance tests; it also records the corrections applied to the first-pass report (GET Host downgraded to P3, build-state wording fixed, the 15/30-minute polling mismatch downgraded to UX-level) and a consolidated nine-step remediation roadmap.
- Verification: Re-ran the full offline suite: all 154 `unittest` tests passed, `python scripts/validate_config.py` passed, and `python -m compileall -q app scripts *.py` passed. Spot-verified the externally corrected claims directly in code before accepting them: `public/app.js:562-568` reloads immediately on visibility change, `app/db.py:486` resets build state on every build start, and `app/sources.py:619,644` future boundaries catch only `RequestException` (with `sources.py:663,683` already catching broader exceptions as corroborating context). No source code was modified in this phase, so no additional tests were required.

## 2026-08-28 15:41:05 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Migrated the 14:30 weekday Telegram premium alert from a Codex cron automation to an independent macOS LaunchAgent. Added a credential-free installer and a launchd runner that checks the China trading calendar before reading Keychain, reuses a healthy local LOF service or starts and cleans up an exact temporary child, passes Telegram credentials only through the report process environment, preserves report exit status and failure notices, and stores no secret in the plist. Installed `local.lof-inav.premium-alert` for the current user and paused the former Codex automation to prevent duplicate delivery.
- Verification: Passed four focused LaunchAgent tests, all 154 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, plist validation, and `git diff --check`. Triggered the installed LaunchAgent through `launchctl`; it independently started a temporary service, refreshed 338 funds and 1,133 quote identifiers, delivered the Telegram report successfully with seven unavailable quote identifiers surfaced, stopped the temporary service, removed its PID file, and recorded launchd exit status 0. Confirmed the installed schedule contains Monday through Friday at 14:30 and the former Codex automation status is `PAUSED`.

## 2026-08-25 15:46:16 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Aligned the scheduled premium/discount Bot report with the page's “屏蔽暂停申购” filter. Threshold selection now excludes only funds whose `purchase_limit.display` is exactly “暂停”, while retaining limited-purchase funds and funds without purchase-limit metadata; the report states that the filter is active, and the operator documentation records the same rule.
- Verification: Passed the nine focused premium-alert tests, configuration validation, all 150 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Completed a live refresh and successfully delivered the filtered Telegram report: 19 paused-purchase funds met the raw 1.5% threshold, zero remained after filtering, and the sent result contained seven premiums plus four discounts. The report surfaced ten unavailable quote identifiers and three affected fund codes as data-quality warnings. Stopped only the temporary validation server and confirmed the pre-existing local service still returned HTTP 200.

## 2026-08-25 11:24:04 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Optimized scheduled premium alerts so the lightweight refresh-status endpoint supplies the local CSRF token, avoiding an automatic list refresh before the explicit NAV and all-quote refresh. Added a read-only `/api/funds?refresh=0` snapshot mode so the final report read cannot launch another background job after a long NAV refresh. The active 14:30 weekday automation reads Telegram credentials from macOS Keychain without displaying them, skips non-trading days, starts the local server only when absent, sends the report, and only stops a server instance that it started.
- Verification: Confirmed both Keychain items exist without reading their values into output. Completed a live all-fund refresh against 338 funds and 1,133 current quote identifiers, delivered an actual Telegram report successfully, and then exercised the optimized path in dry-run mode to confirm one explicit NAV refresh plus one all-quote refresh. Verified a 338-fund read-only snapshot left zero background refreshes active. Passed configuration validation, all 150 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. The live quote refresh reported 35 unavailable identifiers and the report surfaced that partial-data warning rather than hiding it.

## 2026-08-25 10:14:39 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Added a protected force-refresh API for current NAV and valuation quotes, exposing per-kind completion and partial-failure state. Added a China-trading-day alert job that discovers the local port, forces and verifies both refreshes, excludes funds whose NAV refresh failed, reports premiums and discounts beyond the default absolute 1.5% threshold with coverage warnings, and sends either results or sanitized failure notices through generic, Slack, Feishu, WeCom, DingTalk, or Telegram Bot payloads. Documented environment-only credentials and 15:05 macOS/Linux/Windows scheduling.
- Verification: Passed configuration validation, all 145 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, `git diff --check`, CLI help smoke checking, local-target validation, provider-payload tests, refresh coordination tests, report-selection tests, and a secret-pattern scan of all changed files. Live provider refresh and Bot delivery were skipped because no Bot endpoint was supplied and an all-fund network refresh would unnecessarily call rate-limited external data sources; the end-to-end orchestration and failure paths were covered with deterministic HTTP session mocks.

## 2026-08-20 22:30:31 Asia/Shanghai

Device: WUTH-X (Windows 11 家庭版 中文版, AMD64)

- Phase: Mirrored the release-tested Windows x64 Portable startup and Unicode packaging fixes from the private source of truth into the clean sibling `LOF_iNAV_public` repository for the `v1.0.0` GitHub release. The public mirror contains the server/build fixes, focused regression tests, and Windows acceptance record while excluding databases, generated build output, experimental content, and private release tooling.
- Verification: The corrected mirror copied about 1.0 MB of public-safe source and its built-in checks passed `git diff --check`, private/generated-path tracking validation, frontend JavaScript syntax checking, Python compilation, and secret-pattern scanning. In the public repository, the 12 focused Portable/server startup tests passed, `config/fund_rules.json` validated, and all 138 offline `unittest` tests passed. Network-backed refresh behavior and the final executable were already covered by the immediately preceding Windows clean-directory acceptance run rather than repeated during the source mirror.

## 2026-08-20 22:28:09 Asia/Shanghai

Device: WUTH-X (Windows 11 家庭版 中文版, AMD64)

- Phase: Hardened the private-to-public Windows mirror before the first GitHub binary release. The Robocopy source filter now excludes generated `build/` and `dist/` trees plus `.DS_Store`, and the public tracking guard now rejects `build/`, `dist/`, and `experimental/` in addition to data and private workflow paths. Updated both public-release workflow documents to match the executable exclusions.
- Verification: Parsed `private_public_sync/sync_public.ps1` with the PowerShell parser with zero errors, then ran its dry-run mirror with the private worktree override required for this focused edit. The corrected preview reported 77 source files totaling about 1.0 MB, with no `build/` or `dist/` path in the planned copy; the earlier unsafe preview had included about 379 MB of generated output. No public files were changed during either preview.

## 2026-08-20 19:34 Asia/Shanghai

Device: WUTH-X (Windows 11 家庭版 中文版, AMD64)

- Phase: Completed the Windows x64 acceptance run for `LOF_iNAV-Portable-1.0.0-win-x64.zip` and placed the final ZIP plus a SHA-256 sidecar in the user's Downloads directory. The clean extracted package installed its starter database on first launch, opened the browser automatically, served the application, refreshed live data, exported both supported formats, and stopped through the packaged close command.
- Verification: Extracted the final ZIP to the fresh sibling directory `D:\Projects_local\LOF_iNAV_PortableTest_1.0.0_20260820_1921`; confirmed the expected Chinese filenames and absence of a runtime database before launch. The EXE created a ready runtime database, returned HTTP 200, exposed all 338 configured funds through the API, and rendered 338 fund rows after selecting all nine types. The first background refresh checked and changed 338 funds with zero failures and saved 1,122 of 1,123 requested quotes; one quote remained unavailable. The CSV export had a UTF-8 BOM, seven headers, and 338 data rows; the visually reviewed PNG was a valid 1,289 x 16,000 image containing the full table and disclaimer. `关闭 LOF_iNAV.bat` stopped the verified package process and freed port 8001 while a later quote refresh was active; the runtime database still passed `pragma quick_check=ok`, and a second no-op close pass removed the stale PID record left by the force-stop. Microsoft Defender real-time protection was enabled with signature version 1.457.252.0; custom scans of both the ZIP and extracted package reported zero detections. No SmartScreen prompt appeared for this local, unmarked build; the EXE is unsigned, so an Internet-downloaded copy may still receive a reputation warning. Final ZIP size: 28,965,621 bytes. Final SHA-256: `E54835CFBE913C8E698602B2D41E9806F0CFBB8AE7A14B2CDEC8C0CC2BCE3DC2`.

## 2026-08-20 19:20 Asia/Shanghai

Device: WUTH-X (Windows 11 家庭版 中文版, AMD64)

- Phase: Fixed two Windows x64 Portable release blockers found during real packaging. The PowerShell 5.1 build now constructs the Chinese guide and shutdown filenames from Unicode code points so ZIP extraction preserves `使用说明.txt` and `关闭 LOF_iNAV.bat`; the windowed PyInstaller server now routes HTTP access logging through the configured logger and omits the console stream handler when `sys.stderr` is unavailable, preventing every request from ending with an empty response.
- Verification: Passed 12 focused Portable/server startup tests and all 138 offline `unittest` tests. Ran `powershell -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1 -Version 1.0.0` successfully with Python 3.12.10 x64 and PyInstaller 6.22.2; configuration validation passed, the generated seed remained ready with 338 funds and 52,387,840 bytes, and the final win-x64 ZIP was rebuilt. Real extracted-EXE end-to-end verification is recorded separately after the clean-directory acceptance run.

## 2026-08-20 16:08:08 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Regenerated the ignored Windows Portable seed database from the current release-ready development database, retaining seven backtests per fund, and synchronized the complete public-safe tree to the sibling public repository for publication. The generated SQLite database remained under `build/portable_seed/` and was not added to either source repository.
- Verification: The new 52,387,840-byte seed is release-ready with `pragma integrity_check=ok`, a matching configuration hash, latest NAV date 2026-08-19, 338 funds, 67,600 NAV rows, 26,616 holding rows, 383,288 daily-price rows, 2,366 backtest rows, and no fund exceeding seven retained backtests; SHA-256 is `67a1381d35038a3147e88e573d891106e74938c2e3c9e16b15e51fd283b18271`. Passed configuration validation, all 136 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, `git diff --check`, public secret/private/generated-file checks, and post-sync public-safe checksum parity. A Windows x64 ZIP was not built because this device is macOS arm64; final PyInstaller packaging and extracted-package smoke testing remain to be performed on Windows x64.

## 2026-08-13 01:07:26 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Mirrored the private repository's current public-safe source, provider HTTP hardening, dependency pin, focused security tests, and previously completed Portable documentation and shutdown workflow into the sibling `LOF_iNAV_public` repository. Excluded `.git`, `.venv`, `data`, caches, `build`, `dist`, `.DS_Store`, `experimental`, and `private_public_sync`; no generated database, private workflow file, credential, or build artifact was transferred.
- Verification: Previewed the content-based mirror before applying it and confirmed post-sync checksum parity for all public-safe files. In the public repository, passed the focused security tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing, dependency consistency, `git diff --check`, secret-pattern scanning, private/generated path tracking checks, and repeated deadline tests. The 136-test full suite had no functional assertion failures but two pre-existing 120 ms wall-clock threshold tests continued to fail intermittently at 129-131 ms under full-suite load and pass when repeated alone; this non-deterministic timing issue was left unchanged. Network-backed provider refreshes and a Windows executable build were skipped because the changes were covered offline and the current device is macOS.

## 2026-08-13 00:52:10 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Hardened the shared provider HTTP GET and POST helpers against redirect-based blind SSRF and unbounded response consumption. Provider redirects are now rejected before any follow-up request, including body-preserving POST redirects; responses are streamed into the existing `requests.Response` interface under a 16 MiB decoded-body cap, a bounded encoded-input cap for compressed responses, and an absolute per-attempt transfer deadline capped at 30 seconds. Limit failures remain `RequestException`-compatible, close their response, and retain the existing retry behavior. Pinned the directly used urllib3 response API version.
- Verification: Passed focused HTTP-boundary, historical-price deadline, and index-source tests; reproduced rejection of GET 302 and POST 307 redirects, gzip expansion beyond the decoded limit, a continuously delivered body beyond its deadline, and the deadline-specific exception when a real local socket read expires; confirmed successful `.content`, `.text`, and `.json()` consumers plus limit-failure retries. Passed the offline `unittest` suite, configuration validation, Python compilation, frontend JavaScript syntax checking, dependency consistency, and `git diff --check`; two pre-existing timing-threshold tests were each observed to fail intermittently by only 3-10 ms and pass on rerun, without a code-path assertion failure. The patch tool's sandbox separately could not run two unrelated server port-conflict tests because it denied loopback socket binding. Live provider calls were skipped to avoid unnecessary external requests and rate limits; the security boundary was additionally exercised against a deterministic local HTTP server.

## 2026-08-07 19:43:12 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Expanded the Windows Portable user guide with the project's free/open-source status and practical MIT license permissions and obligations; strengthened the no-warranty and personal investment loss disclaimer; explained the latest-published-NAV, holding/manual-proxy, weighted-return, FX, regular stock-session, and continuously traded commodity valuation model; clarified that commodity categories are thematic rather than literal holdings classifications; and documented the exact current-filter/current-sort/current-column scope and limitations of CSV and PNG exports.
- Verification: Passed the focused Portable guide contract tests, all 129 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Network-backed refreshes and a Windows build were skipped because this phase changes user documentation and its offline contract test only.

## 2026-08-07 19:12:52 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Replaced the minimal Windows Portable note with a plain-language end-user guide covering preparation, complete extraction, first-run seed initialization, routine use, status meanings, correct shutdown, independent-folder upgrades, backup/removal, troubleshooting, privacy, and the valuation disclaimer. Added a double-click close command and packaged its verified PID/port-aware PowerShell helper so closing the browser is no longer the only visible user action.
- Verification: Passed the two focused Portable guide/package contract tests, all 129 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing for the build and stop scripts, and `git diff --check`. A Windows x64 ZIP was not rebuilt because the current device is macOS; the actual package assembly remains gated by the existing Windows x64 build guard.

## 2026-08-07 15:10:51 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Mirrored the private repository's current public-safe source, fund rules, Portable packaging workflow, documentation, and tests into the sibling `LOF_iNAV_public` repository. Used the documented exclusions for `.git/`, `.venv/`, `data/`, caches, `build/`, `dist/`, `experimental/`, and `private_public_sync/`; no generated database, report PDF, build artifact, or private workflow file was transferred.
- Verification: Confirmed both repositories were clean before synchronization; previewed the mirror with `rsync --dry-run --delete`; passed the public repository's configuration validation, all 127 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, `git diff --check`, secret-pattern scanning, private-path tracking checks, generated-file extension checks, and post-sync checksum parity for all public-safe files. Network-backed refreshes and a Windows executable build were skipped because the synchronized source changes were covered offline and the current device is macOS.

## 2026-08-07 15:06:30 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Unified the default full backtest window at seven NAV rows across the valuation function, all-fund build, single-fund import, CLI help, and Portable seed retention. Preserved explicit `--days` and `--backtest-rows` overrides for longer manual runs, and named the separate 30-row UI/API display ceiling so manually generated history remains visible without being mistaken for a 30-row build default.
- Verification: Passed 22 focused backtest, CLI, and Portable seed tests plus all 127 offline `unittest` tests; confirmed the three command-line help screens report the seven-row default; passed configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Network-backed builds were skipped because this change only alters bounded defaults and is fully covered by offline tests.

## 2026-08-07 14:58:28 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Replaced the Windows single-file packaging path with a versioned PyInstaller `onedir` Portable ZIP workflow. Added release-ready seed database generation with bounded NAV, holding, price, and backtest retention; removed machine-specific diagnostic metadata; added integrity/readiness gates and atomic output replacement; installed the packaged seed atomically on first launch without overwriting an existing user database; and documented the independent-folder upgrade workflow and package contents.
- Verification: Passed all 123 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing/non-Windows guard checks, and `git diff --check`. Built a macOS PyInstaller `onedir` smoke artifact and confirmed the executable plus bundled frontend, rules, and seed paths. Ran the seed trimmer against a release-state simulation of the 338-fund local cache: it remained ready with `pragma integrity_check=ok`, retained 67,600 NAVs, 26,616 holdings, 378,100 daily prices, all 338 announcements and purchase-limit rows, and compacted to 51,519,488 bytes. Confirmed the unmodified local database is correctly rejected because its configuration hash is pending after the latest fund-rule change, with no partial seed left behind. A real Windows x64 ZIP was not produced on this macOS device; the current database must first complete a release-ready build, then the final script must run on Windows x64.

## 2026-08-07 14:28 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Replaced fund 160216's automatic 2026Q2 DBC fallback with the quarterly report's directly held top-ten fund basket at 69.28% total nominal weight. Preserved UGL, ZSL, TMF, and SCO as the held daily-reset products, added Yahoo fallback symbols for all ten ETFs, and confirmed that all 20 `proxy_then_manual_replace` funds now have manual holdings for their latest database quarter.
- Verification: Passed `python3 scripts/validate_config.py`, 15 focused configuration and price-source tests, all 118 offline `unittest` tests, Python compilation, and `git diff --check`. Ran `.venv/bin/python import_fund.py 160216 --current-only` successfully: the Eastmoney batch source was unavailable through the configured proxy, Yahoo fallback supplied all ten quotes, the latest SQLite snapshot contains the ten disclosed ETFs at 69.28%, and each has current quote data plus daily prices from at least 2026-07-22. Backtesting was intentionally skipped by `--current-only`.

## 2026-08-07 14:14 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Audited fund 160216 against the locally downloaded 2026Q1 and 2026Q2 quarterly reports without changing its Q1 manual configuration. Confirmed that the Q2 report contains a materially different mix of direct commodity, inverse/leveraged, long-duration Treasury, base-metals, exploration-and-production, and oil-services funds; also identified that the current `proxy_then_manual_replace` fallback has already produced a Q2 database snapshot consisting solely of DBC at 79.44%, rather than continuing to use the Q1 manual basket.
- Verification: Visually rendered and reviewed report pages 10-12 for both `data/160216_2026q1.pdf` and `data/160216_2026q2.pdf`; cross-checked their extracted text; inspected the 160216 rule, builder fallback, valuation snapshot selection, git history, current SQLite holdings and backtests; and passed `python3 scripts/validate_config.py`. Network-backed checks were skipped because the original reports and current generated database snapshot were already available locally.

## 2026-08-07 13:47 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Added the macOS `.DS_Store` metadata file to the repository ignore rules.
- Verification: Confirmed `.DS_Store` is ignored with `git check-ignore`, passed `git diff --check`, and verified no unrelated files were included in the phase.

## 2026-08-06 23:43 Asia/Shanghai

Device: TianhaodeMacBook-Pro (macOS 26.5.2, arm64)

- Phase: Mirrored the private repository's public-safe source, configuration, documentation, scripts, and tests into the sibling `LOF_iNAV_public` repository using the documented exclusions for `.git/`, `.venv/`, `data/`, `__pycache__/`, `experimental/`, `private_public_sync/`, and `*.pyc`.
- Verification: Confirmed both repositories were clean before synchronization and the public branch matched `origin/main`; previewed the mirror with `rsync --dry-run --delete`; passed `git diff --check`, private-path tracking checks, `python scripts/validate_config.py`, `node --check public/app.js`, `python -m compileall -q app scripts *.py`, all 116 offline `unittest` tests, and a secret-pattern scan with no matches. Network-backed data refreshes were skipped because the synchronized changes were fully covered by offline validation.

## 2026-08-06 23:30:16 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Added repository-specific agent guidelines based on the public sibling while preserving private-repository tests, public-sync boundaries, mandatory phase logging, and automatic focused local commits.
- Verification: Validated `config/fund_rules.json`; ran all 116 `unittest` tests successfully; passed Python compilation, `public/app.js` syntax checking, and `git diff --check -- AGENTS.md`.
