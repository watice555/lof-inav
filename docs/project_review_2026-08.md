# 项目审查基线报告（2026-08）

- 审查基线：commit `3071636`（main，2026-08-28 之后）。
- 方法：四路专项审查（服务端/安全、数据源/网络、构建/数据库、估值/前端）通读全部核心代码；离线验证通过：154 个 `unittest` 全过（0.17s）、`python scripts/validate_config.py` 通过、`python -m compileall` 通过。
- 本报告为第二轮定稿：初版发现经外部复核逐项校验后，修正了三处定级（GET Host 校验降为 P3、build_state 滞留措辞、15/30 分钟轮询降为 UX 项），细化了若干修复方案，并按"复现方式 / 影响 / 置信度 / 验收测试"格式重写。
- 与 `docs/project_review_report.md`（2026-06）的关系：旧报告 P1/P2 已基本修复（单基金降级、POST Host+CSRF、回测 data_quality 标记、`tests/` 目录从无到 26 个文件、per-fund 失败汇总、HTTP 降级已移除）。本报告聚焦当前代码库的剩余问题。

分类框架：

- **A 确认缺陷**：有代码依据与复现路径，应修。
- **B 设计取舍**：口径/产品决策，先定方向再动代码。
- **C 性能假设**：有静态推算依据，改前先基准。
- **D 未来风险**：时间相关，确定性或大概率发生。

---

## A 确认缺陷

### A1（高）日内行情缺少可信度校验

- 位置：`app/valuation.py:566`（`_positive_float` 仅校验正数与有限性）、`app/valuation.py:425`（直接进入收益计算）；`app/sources.py:955-965`（pct 仅在 |pct|>1000 时拒绝）。回测侧异常值检测（`valuation.py:21, 851-854`）只打标签，不拦截。
- 复现：内存 SQLite 构造一条 quotes 行：`price = 10 × previous_close`、`fetch_status='ok'`、`last_success_at` 新鲜 → 该持仓收益放大 10 倍直接进入估值与溢价展示，无拦截无告警；或 mock 回退链（eastmoney→sina→Yahoo）返回错误标的的行情。
- 影响：单条坏行情（错价、错单位、错标的）静默污染估值与溢价——对估值工具是最危险的失败模式。
- 置信度：高（代码路径直接核实）。
- 修复方向：多信号可信度校验——(1) `|price/previous_close − 1|` 超过按资产类别的阈值即判无效；(2) pct 与 `price/previous_close` 交叉一致性校验；(3) 标的市场身份核对；(4) 多源报价冲突检测。异常时标记该持仓 invalid、当轮隐藏溢价、产生数据告警——不静默截断、也不静默使用。
- 验收测试：注入 10× 错价 → 该持仓 unpriced + invalid 告警 + 溢价不展示；注入 ±5% 正常波动 → 无影响；注入 pct 与价格矛盾的行情 → 拒绝。

### A2（中高）`--current-only` 单基金导入后旧回测不失效

- 位置：`import_fund.py:45-57`（无 `pending_backtest_from`、无回测删除）；正确实现对照：`app/build.py:453-520`（服务端 `refresh_reports` 路径设置 `pending_report_backtests` 并触发增量回测）。
- 复现：对已有回测的基金执行 `import_fund.py <code> --current-only` 且新公告持仓快照变化 → `backtests` 中 publish_date 之后的行仍存在且无失效标记，服务端继续按旧持仓的回测展示，数据库自报 ready。
- 影响：换季报后回测数据静默错误。
- 置信度：高。
- 修复方向：失效逻辑收进 `import_fund_data()` 的快照替换内部（按持仓/公告指纹变化自动删除或标记 pending），CLI、`build_all`、`refresh_reports` 共享同一逻辑，避免未来调用方再漏。
- 验收测试：mock 新旧两个快照导入 → 旧回测行删除或 pending 标记；快照未变化 → 不触发。

### A3（中高）行情刷新对解析异常不设防，单批失败报废整轮

- 位置：`app/sources.py:619、644`（special 与 eastmoney 批量两个 future 边界仅捕 `RequestException`）；解析点 `sources.py:779、1054、1091-1098`（`int()`/下标访问可抛 `ValueError`/`KeyError`）；对照：sina 阶段（`sources.py:663`）已捕 `(RequestException, ValueError, IndexError)`、fallback 阶段（`sources.py:683`）已捕 `Exception`；`app/build.py` `refresh_quotes` 调用处无兜底。
- 复现：mock `eastmoney_realtime_batch` 返回 dict 形 diff → `ValueError` 穿出 `fetch_realtime_quotes` → 整轮已抓行情（含其他批次成功结果与 special 标的）全部丢弃，日志仅记 "Quote refresh failed"。
- 影响：上游一次格式漂移即报废整轮行情刷新，已成功的数据一并丢失。
- 置信度：高。
- 修复方向：两层修法——future 边界放宽为 `Exception` 并经 `record_realtime_source_error` 记录诊断（保留部分成功结果）；各解析器内部对明确异常（`ValueError`/`KeyError`/`IndexError`）就地捕获记录，避免宽捕获掩盖编程错误。
- 验收测试：mock 单批格式漂移 → 其他批行情仍写库、诊断含失败批次；mock 正常返回 → 无回归。

### A4（中）申购限额失败中断整个构建，状态滞留 running

- 位置：`app/build.py:193-194`（`build_all` 无守卫调用 `refresh_purchase_limits`）、`build.py:269-297`（记录元数据后 re-raise）、`build.py:266`（`--current-only` 开头 `delete from backtests`）；`app/db.py:398-403`（config hash 不变时 running 无回收机制）。
- 复现：mock `fetch_purchase_limits` 抛错跑 `build_all` → 构建中断于导入之后，回测与收尾全部跳过，`mark_build_finished` 不执行，`build_state` 滞留 running；服务端因 not-ready 拒绝启动。
- 影响：单一外部接口故障导致构建半途中断与状态滞留。注意：可人工恢复（重跑构建即经 `mark_build_started`（`db.py:486`）重置状态），并非不可恢复；但恢复有真实成本——用 `--current-only` 恢复会先删光全部回测（`build.py:266`）。
- 置信度：高。
- 修复方向：(1) 申购限额按阶段捕获，失败记入 `last_build_errors` 而非中断；(2) 构建生命周期增加 failed 顶层状态与 stale-running 心跳回收；(3) `--current-only` 的回测删除移到成功收尾，或改为按需条件删除。
- 验收测试：mock 限额失败 → 构建走完、状态 complete/partial、错误入 `last_build_errors`；模拟构建进程中途死亡 → 下次启动识别 stale running 为 failed 并给出明确指引。

### A5（中）fund_rules.json 运行时零校验

- 位置：`app/config.py:35-53`（裸加载，坏字段直接 KeyError/ValueError traceback）；`scripts/validate_config.py` 校验完备但无任何 app 代码调用；校验盲区：`proxy_weight` 无上限（`build.py:1672` 不钳制配置权重）、重复 `proxy_secids` 静默少配（`build.py:1436-1454` insert or replace 折叠）、manual 权重和仅拒 >1.5（`validate_config.py:182`）、非正权重被 `add_single_holding` 静默丢弃（`build.py:1457-1469`）。
- 复现：`fund_rules.json` 删掉某基金必填字段 → `import app.config` 即 traceback，整个应用无法启动且报错不友好；`proxy_weight` 写 1.2 → 无拦截进入 holdings 并放大估值。
- 影响：手工编辑配置的隐性错误直达构建与估值。
- 置信度：高。
- 修复方向：纯校验逻辑移入 `app/`（如 `app/config_validation.py`），`load_funds()` 强制执行并抛可读的 `ConfigError`；CLI 复用同一实现。权重 >1 不一刀切禁止——配置中已有有解释的等效敞口实例（164701 某期单持仓 1.0141、160723 某期合计 1.0555）——改为要求显式标注（rationale 字段或 allow_leverage 标志）方可通过；同时补 `proxy_weight ∈ (0,1]`、重复 proxy_secids 拒绝、非正权重报错。
- 验收测试：坏配置 → 启动时输出清晰错误列表；权重>1 无标注 → 校验失败；有标注 → 通过；重复 proxy_secids → 校验失败。

### A6（低）已确认的次要缺陷清单

- `public/app.js:1298` 回测日期未过 `escapeHtml`（其余插值均已转义）。
- `/api/refresh-status` 响应缺 `backtest_status`/`backtests_disabled`，首次加载若恰逢刷新中，回测范围短暂显示"无数据"（`server.py:105-138` vs `app.js:534`）。
- 错误路径基金 payload 的 name 用 code 顶替（`server.py:301`）。
- `import_fund.py:48` 单基金导入会刷新全部 338 只基金的申购限额。
- `scripts/stop_server.ps1:6` `SilentlyContinue` 掩盖 cmdlet 错误。
- `refresh_reports` 对 api.fund.eastmoney.com 8 线程无间隔突发 338+ 请求（`build.py:385-389`）；Yahoo 回退优先级最高且携带 fund.eastmoney.com 的 Referer（`sources.py:331-332` 未按域区分），属限流礼貌性问题。

---

## B 设计取舍（先定方向，再动代码）

### B1 低覆盖估值的展示策略

- 概念辨析：`modeled_weight` = 模型解释了多少基金资产；`priced_ratio` = 已建模部分中多少成功计价。两者 UI 均已展示（`public/app.js:287-295`），回测另有 0.6 最低计价比（`valuation.py:20, 849`）。实时估值无最低覆盖门槛，低计价基金的估值与溢价仍常规展示。
- 决策点：是否引入置信度标签（低覆盖样式）；极低置信度时是否隐藏溢价。不建议直接套用回测 0.6 阈值——这是产品策略而非工程缺陷。
- 关联：与 A1 联动（invalid 行情应进入同一置信度展示通道）。

### B2 杠杆/等效敞口权重的显式标注

- 决策点：rationale 字段 vs allow_leverage 标志（与 A5 校验器联动）。涉及存量实例：164701、160723。

### B3 未覆盖资产按 0% 收益处理

- 现状：缺价持仓按现金（0 收益）处理且不缩放（`valuation.py:264, 284-286`）。保守方向的取舍；建议在 README/docs 明示口径，可选提供比例缩放模式。

### B4 FX 无节假日日历

- `market_calendar.py:164-179` 对市场 120 返回 None，CN 长假期间 FX 日线判 stale → 外币持仓 unpriced + 噪声告警。方向保守不算错；可后续把 FX 映射 CN 日历降低噪声。

---

## C 性能假设（改前先基准）

### C1 全量/当前构建的串行导入（构建耗时主项）

- 依据：`build.py:149-157` 串行循环 338 只 × 每只约 7 次串行请求 ≈ 2400 次往返，为静态推算的理论上界（单请求默认 20s 超时 × 3 次尝试），非实测。
- 方案：网络阶段有界并发（2-4 worker、按域名限速），写库保持串行保存点（网络 I/O 与写事务已分离，`build.py:1114-1116`，可安全并行）；`refresh_reports` 的 8-worker 模式（`build.py:385`）已验证该模式可行，但导入并发度需实测限流情况后再定。
- 验收：基准脚本记录改前/改后各阶段耗时；并发下无新增限流错误。

### C2 HTTP 连接复用

- 依据：`sources.py:386-391` 每请求新建 TCP+TLS（模块级无 Session）；行情刷新每分钟数百请求。
- 方案：不用共享单 Session（`requests.Session` 非线程安全，且有跨域 Referer 问题）——线程本地 Session 或按域 Session + `HTTPAdapter` 连接池 + 按域默认头（Yahoo 不带 fund.eastmoney.com Referer）。预估 20-30 行，而非"三行"。
- 验收：单测覆盖多线程并发抓取无异常；基准对比握手开销。

### C3 `missing_price_targets` N+1

- 依据：`build.py:2455-2504` 每个 (secid, date) 一次 SELECT，全量构建约 4 万次且跑两遍（fetch 后再算 unresolved），为静态推算。
- 方案：每 secid 一次查询取日期列表 + Python 二分，或临时表 join。先在真实库基准确认收益再合并。
- 验收：基准数据（改前/改后查询数与耗时）+ 现有测试全过。

### C4 其余小项（低风险）

- `/api/funds` 缓存 miss 无 single-flight（并发标签页各自全量计算，`server.py:213-224`）；payload 缓存可同时缓存序列化字节。
- 静态资源 `no-store`（`server.py:209-211`），静态路径放行短缓存。
- `/api/refresh-status` 每次轮询约 13 次 `get_meta` 可合并；进度字典重复构建（`server.py:676-684` vs `787-788`）。
- 15 分钟行情过期窗（`valuation.py:24`）vs 30 分钟隐藏页轮询（`app.js:75, 522`）：回前台立即刷新（`app.js:562-568` 已核实）但首帧可能仍为降级数据、约 1 分钟自愈——UX 级，对齐阈值即可（初版报告列为中危，定级偏高）。
- 刷新调度 5 份约 250 行样板去重（`server.py:1385-1695`）；`refresh_daily_prices`/`refresh_mark_prices` 各有一对近重复实现，其中 `refresh_daily_prices`（`build.py:1916-2038`）与 `refresh_mark_prices`（`build.py:2226-2303`）为生产死代码；`backtest_summary` 双实现（`valuation.py:1022` / `server.py:978`）；`incremental_backtest_anchor_date` 双实现（`build.py:1088` / `valuation.py:802`）；`price_only_codes` 恒为 `[]` 的死分支（`build.py:716` 等）。
- 请求处理无兜底 try/except（handler 异常直接断连，浏览器见网络错误而非 5xx，`server.py:86-152`）。

---

## D 未来风险

### D1（中，确定性时间炸弹）CN 节假日表 2026-12-31 到期

- 位置：`market_calendar.py:85`（CN_HOLIDAYS 2024-2026；HK 已含 2027）、`:145`（`KNOWN_MARKET_CALENDAR_YEARS` 仅告警脚本使用：`scripts/report_premium_alerts.py:127`、`scripts/run_premium_alert_macos.py:209`；server/valuation 路径不检查）。
- 复现：`is_trading_session("CN", <2027 法定节假日>)` → 误判为交易日；`expected_market_closure_gap` 对节前缺口返回 None → 价格误判 stale。
- 影响：2027-01-01 起（约 4 个月）产生一波 unpriced 持仓与误告警。
- 置信度：高（确定性）。
- 修复方向：补 2027 CN 表；核心估值路径对未知年份显式返回"日历未知"并降级/告警，而非按普通工作日运行。
- 验收测试：2027 已知节假日 → 正确闭市；未知年份日期 → 明确的未知信号（非静默按工作日）。

### D2（中高，机制确认、污染面待测）前复权缓存漂移

- 位置：`sources.py:1289、1303`（`fqt: "1"` 前复权）；`build.py:2455-2504`（已有行视为永久新鲜，只补缺失日期）；本可缓解的 7 天回补逻辑位于 `refresh_daily_prices`（`build.py:1916-2038`）——生产死代码。
- 机制：持仓除权后前复权历史整段平移基准，缓存行不重取 → 同一 secid 混两套复权基准，缓存边界虚假跳变 → 回测收益错。
- 置信度：机制高；实际污染面需回归测试量化（哪些 secid 已处于混基准状态）。
- 修复方向：先写"除权前后两次抓取同一 secid"回归测试证明，再实现按 secid 原子刷新 + 缓存代际（整段同基准替换）。不做局部日期补丁，也不切换不复权口径。
- 验收测试：回归测试先红后绿；修复后同 secid 全段一致基准。

---

## 已验证无问题（正面清单）

- FX 收益合成方向（收益合成天然免疫方向约定错误）。
- 溢价一律对日内估值计算，无"活价对死净值"配对（含 `premium_alerts`）。
- 时区固定 Asia/Shanghai / America/New_York（`quote_session_date`），与机器时区无关。
- POST Host+CSRF 校验、参数化 SQL、无 CORS 头、静态文件经 stdlib `translate_path` 无路径穿越。
- WAL + 每请求短连接、网络 I/O 严格在写事务外（有专门测试验证）。
- 出站请求全链路超时 / 30s 绝对期限 / 16MB 上限 / 拒绝重定向 / 有界重试。
- 前端轮询定时器单槽管理无泄漏、渲染竞态 token 化、详情请求 AbortController。
- GET Host 校验缺失：经复核降级为 P3 纵深防御（数据非敏感、POST 已受 Host+CSRF 保护、GET 触发的后台刷新有冷却与锁兜底）；修复成本约 2 行，随 C4 批次处理。

## 修复路线图（合并后优先级）

1. A1 日内行情可信度校验（含 B1 决策联动）
2. D1 2027 CN 日历 + 未知年份核心路径保护
3. A2 持仓快照与回测失效集中管理
4. D2 前复权漂移回归测试 → 按 secid 原子刷新 + 缓存代际
5. A4 构建生命周期（失败状态、恢复、申购限额阶段隔离）
6. A3 行情 future 保留部分成功 + C1 NAV 整批 deadline
7. A5 运行时配置校验（含 B2 决策联动）
8. C2/C3/C4 性能工作（基准先行）
9. A6 + GET Host + 静态缓存 + 代码去重等低风险整理

与初版报告的差异记录：GET Host 中→P3；"永久卡死"修正为"滞留至人工重跑构建，恢复路径含破坏性删除"；15/30 分钟项中→UX 级；"30-50% 阈值"细化为多信号校验；"6 小时"标注为理论上界；Session"三行"修正为线程本地/按域方案；N+1"降一个数量级"降为待基准验证的推算。

## 开工前需要用户决策

- B1：低覆盖展示策略（置信度标签 / 隐藏溢价阈值）。
- B2：杠杆权重标注机制（rationale 字段 / allow_leverage）。
