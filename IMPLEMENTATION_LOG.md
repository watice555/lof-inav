# Implementation Log

## 2026-08-06 23:30:16 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Added repository-specific agent guidelines based on the public sibling while preserving private-repository tests, public-sync boundaries, mandatory phase logging, and automatic focused local commits.
- Verification: Validated `config/fund_rules.json`; ran all 116 `unittest` tests successfully; passed Python compilation, `public/app.js` syntax checking, and `git diff --check -- AGENTS.md`.

## 2026-08-06 23:43 Asia/Shanghai

Device: TianhaodeMacBook-Pro (macOS 26.5.2, arm64)

- Phase: Mirrored the private repository's public-safe source, configuration, documentation, scripts, and tests into the sibling `LOF_iNAV_public` repository using the documented exclusions for `.git/`, `.venv/`, `data/`, `__pycache__/`, `experimental/`, `private_public_sync/`, and `*.pyc`.
- Verification: Confirmed both repositories were clean before synchronization and the public branch matched `origin/main`; previewed the mirror with `rsync --dry-run --delete`; passed `git diff --check`, private-path tracking checks, `python scripts/validate_config.py`, `node --check public/app.js`, `python -m compileall -q app scripts *.py`, all 116 offline `unittest` tests, and a secret-pattern scan with no matches. Network-backed data refreshes were skipped because the synchronized changes were fully covered by offline validation.

## 2026-08-07 13:47 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Added the macOS `.DS_Store` metadata file to the repository ignore rules.
- Verification: Confirmed `.DS_Store` is ignored with `git check-ignore`, passed `git diff --check`, and verified no unrelated files were included in the phase.

## 2026-08-07 14:14 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Audited fund 160216 against the locally downloaded 2026Q1 and 2026Q2 quarterly reports without changing its Q1 manual configuration. Confirmed that the Q2 report contains a materially different mix of direct commodity, inverse/leveraged, long-duration Treasury, base-metals, exploration-and-production, and oil-services funds; also identified that the current `proxy_then_manual_replace` fallback has already produced a Q2 database snapshot consisting solely of DBC at 79.44%, rather than continuing to use the Q1 manual basket.
- Verification: Visually rendered and reviewed report pages 10-12 for both `data/160216_2026q1.pdf` and `data/160216_2026q2.pdf`; cross-checked their extracted text; inspected the 160216 rule, builder fallback, valuation snapshot selection, git history, current SQLite holdings and backtests; and passed `python3 scripts/validate_config.py`. Network-backed checks were skipped because the original reports and current generated database snapshot were already available locally.

## 2026-08-07 14:28 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Replaced fund 160216's automatic 2026Q2 DBC fallback with the quarterly report's directly held top-ten fund basket at 69.28% total nominal weight. Preserved UGL, ZSL, TMF, and SCO as the held daily-reset products, added Yahoo fallback symbols for all ten ETFs, and confirmed that all 20 `proxy_then_manual_replace` funds now have manual holdings for their latest database quarter.
- Verification: Passed `python3 scripts/validate_config.py`, 15 focused configuration and price-source tests, all 118 offline `unittest` tests, Python compilation, and `git diff --check`. Ran `.venv/bin/python import_fund.py 160216 --current-only` successfully: the Eastmoney batch source was unavailable through the configured proxy, Yahoo fallback supplied all ten quotes, the latest SQLite snapshot contains the ten disclosed ETFs at 69.28%, and each has current quote data plus daily prices from at least 2026-07-22. Backtesting was intentionally skipped by `--current-only`.

## 2026-08-07 14:58:28 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Replaced the Windows single-file packaging path with a versioned PyInstaller `onedir` Portable ZIP workflow. Added release-ready seed database generation with bounded NAV, holding, price, and backtest retention; removed machine-specific diagnostic metadata; added integrity/readiness gates and atomic output replacement; installed the packaged seed atomically on first launch without overwriting an existing user database; and documented the independent-folder upgrade workflow and package contents.
- Verification: Passed all 123 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing/non-Windows guard checks, and `git diff --check`. Built a macOS PyInstaller `onedir` smoke artifact and confirmed the executable plus bundled frontend, rules, and seed paths. Ran the seed trimmer against a release-state simulation of the 338-fund local cache: it remained ready with `pragma integrity_check=ok`, retained 67,600 NAVs, 26,616 holdings, 378,100 daily prices, all 338 announcements and purchase-limit rows, and compacted to 51,519,488 bytes. Confirmed the unmodified local database is correctly rejected because its configuration hash is pending after the latest fund-rule change, with no partial seed left behind. A real Windows x64 ZIP was not produced on this macOS device; the current database must first complete a release-ready build, then the final script must run on Windows x64.

## 2026-08-07 15:06:30 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Unified the default full backtest window at seven NAV rows across the valuation function, all-fund build, single-fund import, CLI help, and Portable seed retention. Preserved explicit `--days` and `--backtest-rows` overrides for longer manual runs, and named the separate 30-row UI/API display ceiling so manually generated history remains visible without being mistaken for a 30-row build default.
- Verification: Passed 22 focused backtest, CLI, and Portable seed tests plus all 127 offline `unittest` tests; confirmed the three command-line help screens report the seven-row default; passed configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Network-backed builds were skipped because this change only alters bounded defaults and is fully covered by offline tests.

## 2026-08-07 15:10:51 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Mirrored the private repository's current public-safe source, fund rules, Portable packaging workflow, documentation, and tests into the sibling `LOF_iNAV_public` repository. Used the documented exclusions for `.git/`, `.venv/`, `data/`, caches, `build/`, `dist/`, `experimental/`, and `private_public_sync/`; no generated database, report PDF, build artifact, or private workflow file was transferred.
- Verification: Confirmed both repositories were clean before synchronization; previewed the mirror with `rsync --dry-run --delete`; passed the public repository's configuration validation, all 127 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, `git diff --check`, secret-pattern scanning, private-path tracking checks, generated-file extension checks, and post-sync checksum parity for all public-safe files. Network-backed refreshes and a Windows executable build were skipped because the synchronized source changes were covered offline and the current device is macOS.

## 2026-08-07 19:12:52 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Replaced the minimal Windows Portable note with a plain-language end-user guide covering preparation, complete extraction, first-run seed initialization, routine use, status meanings, correct shutdown, independent-folder upgrades, backup/removal, troubleshooting, privacy, and the valuation disclaimer. Added a double-click close command and packaged its verified PID/port-aware PowerShell helper so closing the browser is no longer the only visible user action.
- Verification: Passed the two focused Portable guide/package contract tests, all 129 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing for the build and stop scripts, and `git diff --check`. A Windows x64 ZIP was not rebuilt because the current device is macOS; the actual package assembly remains gated by the existing Windows x64 build guard.

## 2026-08-07 19:43:12 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Expanded the Windows Portable user guide with the project's free/open-source status and practical MIT license permissions and obligations; strengthened the no-warranty and personal investment loss disclaimer; explained the latest-published-NAV, holding/manual-proxy, weighted-return, FX, regular stock-session, and continuously traded commodity valuation model; clarified that commodity categories are thematic rather than literal holdings classifications; and documented the exact current-filter/current-sort/current-column scope and limitations of CSV and PNG exports.
- Verification: Passed the focused Portable guide contract tests, all 129 offline `unittest` tests, configuration validation, Python compilation, frontend JavaScript syntax checking, and `git diff --check`. Network-backed refreshes and a Windows build were skipped because this phase changes user documentation and its offline contract test only.

## 2026-08-13 00:52:10 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Hardened the shared provider HTTP GET and POST helpers against redirect-based blind SSRF and unbounded response consumption. Provider redirects are now rejected before any follow-up request, including body-preserving POST redirects; responses are streamed into the existing `requests.Response` interface under a 16 MiB decoded-body cap, a bounded encoded-input cap for compressed responses, and an absolute per-attempt transfer deadline capped at 30 seconds. Limit failures remain `RequestException`-compatible, close their response, and retain the existing retry behavior. Pinned the directly used urllib3 response API version.
- Verification: Passed focused HTTP-boundary, historical-price deadline, and index-source tests; reproduced rejection of GET 302 and POST 307 redirects, gzip expansion beyond the decoded limit, a continuously delivered body beyond its deadline, and the deadline-specific exception when a real local socket read expires; confirmed successful `.content`, `.text`, and `.json()` consumers plus limit-failure retries. Passed the offline `unittest` suite, configuration validation, Python compilation, frontend JavaScript syntax checking, dependency consistency, and `git diff --check`; two pre-existing timing-threshold tests were each observed to fail intermittently by only 3-10 ms and pass on rerun, without a code-path assertion failure. The patch tool's sandbox separately could not run two unrelated server port-conflict tests because it denied loopback socket binding. Live provider calls were skipped to avoid unnecessary external requests and rate limits; the security boundary was additionally exercised against a deterministic local HTTP server.

## 2026-08-13 01:07:26 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Mirrored the private repository's current public-safe source, provider HTTP hardening, dependency pin, focused security tests, and previously completed Portable documentation and shutdown workflow into the sibling `LOF_iNAV_public` repository. Excluded `.git`, `.venv`, `data`, caches, `build`, `dist`, `.DS_Store`, `experimental`, and `private_public_sync`; no generated database, private workflow file, credential, or build artifact was transferred.
- Verification: Previewed the content-based mirror before applying it and confirmed post-sync checksum parity for all public-safe files. In the public repository, passed the focused security tests, configuration validation, Python compilation, frontend JavaScript syntax checking, PowerShell parsing, dependency consistency, `git diff --check`, secret-pattern scanning, private/generated path tracking checks, and repeated deadline tests. The 136-test full suite had no functional assertion failures but two pre-existing 120 ms wall-clock threshold tests continued to fail intermittently at 129-131 ms under full-suite load and pass when repeated alone; this non-deterministic timing issue was left unchanged. Network-backed provider refreshes and a Windows executable build were skipped because the changes were covered offline and the current device is macOS.

## 2026-08-20 16:08:08 Asia/Shanghai

Device: TianhaodeMacBook-Pro.local (macOS 26.5.2, arm64)

- Phase: Regenerated the ignored Windows Portable seed database from the current release-ready development database, retaining seven backtests per fund, and synchronized the complete public-safe tree to the sibling public repository for publication. The generated SQLite database remained under `build/portable_seed/` and was not added to either source repository.
- Verification: The new 52,387,840-byte seed is release-ready with `pragma integrity_check=ok`, a matching configuration hash, latest NAV date 2026-08-19, 338 funds, 67,600 NAV rows, 26,616 holding rows, 383,288 daily-price rows, 2,366 backtest rows, and no fund exceeding seven retained backtests; SHA-256 is `67a1381d35038a3147e88e573d891106e74938c2e3c9e16b15e51fd283b18271`. Passed configuration validation, all 136 offline `unittest` tests, Python compilation, frontend JavaScript syntax checking, `git diff --check`, public secret/private/generated-file checks, and post-sync public-safe checksum parity. A Windows x64 ZIP was not built because this device is macOS arm64; final PyInstaller packaging and extracted-package smoke testing remain to be performed on Windows x64.
