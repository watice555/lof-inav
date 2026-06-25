# 项目审阅报告

审阅日期：2026-06-24

本报告基于只读审阅和若干子代理专项审阅整理，覆盖项目结构、业务目标、代码质量、架构、安全、依赖、测试、性能、用户体验和开发体验。审阅过程中未修改源码；轻量验证包括 Python 语法编译和前端 JavaScript 语法检查。

## 1. 项目概览

LOF iNAV 是一个本地 LOF 日内估值实验台。项目从东方财富、新浪财经、Yahoo Finance 等公开数据源抓取基金净值、公告持仓、行情、汇率、申购状态、历史日线和回测数据，写入本地 SQLite 缓存，再通过本地 HTTP 服务和静态前端展示估算 iNAV、折溢价、持仓代理和回测误差。

技术栈较轻：Python 标准库 HTTP server、SQLite、requests、pandas、BeautifulSoup/lxml，以及原生 HTML/CSS/JavaScript。主要入口是 `quick_start.bat`、`build.py`、`serve.py`、`import_fund.py`；核心模块是 `app/sources.py`、`app/build.py`、`app/valuation.py`、`app/server.py` 和 `public/app.js`。

核心数据流：

1. `app/config.py` 导入时读取 `config/fund_rules.json`，生成全局基金规则。
2. `app/build.py` 抓取净值、公告、持仓、行情和日线，写入 SQLite。
3. `app/valuation.py` 基于最新净值、最新可用持仓、实时行情和汇率估算 iNAV，并执行回测。
4. `app/server.py` 提供静态文件和 JSON API，并在请求触发下调度后台刷新。
5. `public/app.js` 拉取 API，完成筛选、排序、详情展示和 CSV/PNG 导出。

## 2. 总体评价

项目目标清晰，README 对运行方式、估值逻辑、回测口径、代理规则和免责声明说明充分。对于个人研究型金融工具来说，当前结构已经覆盖了完整的数据闭环。

主要风险集中在可靠性和数据质量：单只基金或单个外部源异常可能影响整页可用性；低覆盖回测会进入摘要指标；部分数据源失败时缺少足够可追踪的错误元数据。

架构上，目前的单文件模块仍可维护，但已经接近需要拆分的边界。`app/server.py` 同时负责 API、静态服务、后台调度和数据警报；`public/app.js` 同时负责状态、渲染、交互和导出；`app/sources.py` 承载大量外部源协议。

安全上未发现个人密钥泄露。需要优先处理的是本地 POST 缺少跨站触发防护、行情请求存在 HTTP 降级、CSV 导出可能被 Excel 公式注入影响等问题。

测试是最大短板。项目目前缺少自有测试目录、测试入口和 CI 式 smoke check，后续每次改估值、配置或数据源逻辑都容易出现回归。

## 3. 关键问题列表

| 优先级 | 类型 | 位置 | 问题说明 | 影响 | 建议修复方式 |
| --- | --- | --- | --- | --- | --- |
| P1 | Bug/可靠性 | `app/server.py::Handler.handle_funds`, `app/valuation.py::estimate_intraday` | 配置中单只基金缺 DB 行会抛 `KeyError`，可能让 `/api/funds` 整体失败 | 页面不可用 | 按基金粒度捕获并返回降级项 |
| P1 | 测试/业务 | `app/valuation.py::calculate_backtest_row`, `backtest_summary` | 回测只要 `covered_weight > 0` 就进入 MAE 统计 | 低覆盖样本污染指标 | 增加质量标记和最低覆盖阈值 |
| P1 | 性能 | `app/server.py::handle_funds`, `collect_data_alerts` | 每次列表刷新对 338 只基金产生大量 N+1 SQLite 查询 | 列表延迟随规模增长 | 批量预取和缓存回测诊断 |
| P1 | 性能/可靠性 | `app/build.py::build_all`, `app/db.py::connect` | 长事务中混合大量网络 I/O | 写锁等待，失败回滚大量进度 | 网络抓取和数据库写入分离，按基金或批次提交 |
| P2 | 安全 | `app/server.py::do_POST` | 本地 POST 无 Origin/CSRF 校验 | 恶意网页可触发本机增量回测 | 校验 Host/Origin，增加 CSRF token |
| P2 | 安全/数据完整性 | `app/sources.py::_get` | HTTPS 失败后自动降级 HTTP | 行情可被篡改污染估值 | 移除 HTTP fallback |
| P2 | 配置 | `app/config.py::load_funds` | `fund_rules.json` 缺少 schema 校验 | 新增基金易出隐性错误 | 增加校验命令 |
| P2 | Bug/配置 | `app/config.py` | 路径依赖当前工作目录 | 从非项目根运行会失败 | 使用项目根绝对路径并支持环境变量覆盖 |
| P2 | 可靠性 | `app/build.py::build_all` | 全量构建中任一基金失败会中断 | 批量导入脆弱 | per-fund failure 汇总 |
| P2 | 数据质量 | `app/sources.py::_best_daily_price_rows` | 候选源都未通过校验时仍返回最长 rows | 异常日线可能入库 | 只持久化已验证数据并记录失败原因 |
| P2 | 架构 | `app/market_calendar.py` | CN/HK 节假日硬编码到 2026 | 未来/历史诊断误判 | 可更新日历或未知年份告警 |
| P3 | 安全/前端 | `public/app.js::csvEscape` | CSV 未处理 Excel 公式注入 | 打开导出文件存在风险 | 对公式前缀加安全前缀 |
| P3 | DX/依赖 | `requirements.txt` | 只锁直接依赖，无 Python 版本/哈希 lock | 环境不可复现 | 增加 lock 文件或明确 Python 版本 |
| P3 | 测试 | 仓库整体 | 缺少测试目录和测试配置 | 回归风险高 | 新建 pytest 与 smoke tests |

## 4. 高风险问题详细说明

### 4.1 `/api/funds` 单点失败

`Handler.handle_funds` 对 `FUNDS` 中每只基金直接调用 `estimate_intraday`。当 SQLite `funds` 表缺少某只基金时，`estimate_intraday` 会抛出 `KeyError`。新增基金未构建、单只导入失败或缓存不完整时，整个列表接口可能 500。应改为按基金粒度降级，保留其他基金展示。

### 4.2 低覆盖回测污染指标

`calculate_backtest_row` 在 `covered_weight > 0` 时保存回测结果，未覆盖资产等价于无收益贡献。对代理不完整或价格缺失的基金，MAE、最新误差和最大误差可能失真。应保存 `data_quality` 或 `is_low_coverage`，并在摘要中排除低覆盖样本。

### 4.3 列表接口 N+1 查询

`/api/funds` 每次刷新都逐基金查询净值、行情、持仓和回测摘要，随后数据警报再逐基金查询回测诊断。前端每 60 秒刷新一次，基金规模继续增长后容易造成接口延迟。应批量预取列表所需数据，并把重诊断缓存到后台任务。

### 4.4 长事务混合网络 I/O

`build_all` 在一个 SQLite 连接上下文中执行大量网络抓取和数据库写入，直到结束才提交。全量构建较慢时，这会造成长事务、写锁等待和失败后大段进度丢失。应将网络抓取和数据库提交拆分，按基金或阶段提交。

### 4.5 本地 POST 可被跨站触发

服务绑定在 `127.0.0.1`，但浏览器中的任意网页仍可尝试向本机发起简单 POST。`/api/backtests/incremental` 会启动本地网络和数据库任务，缺少 Origin/CSRF 校验。应给页面下发本地随机 token，并要求 POST 带自定义头。

### 4.6 HTTPS 降级 HTTP

`sources._get` 对部分东方财富 `push2` 接口在 HTTPS 失败后自动重试 HTTP。金融估值工具应优先保证数据完整性，行情请求失败应显式告警，而不是静默接受可被篡改的数据。

## 5. 建议的改进路线

### 立即修复

1. 隔离 `/api/funds` 单只基金失败。
2. 移除行情请求 HTTP 降级。
3. 给本地 POST 增加 Host/Origin/CSRF 校验。
4. 为低覆盖回测增加质量标记和摘要排除逻辑。
5. 补充 `.gitignore` 的 `.env*`、`*.log`、`*.sqlite3*`、`*.db*` 等生成物规则。

### 短期改进

1. 新增配置 schema 校验命令。
2. 将 `config.py` 路径改为项目根绝对路径，并支持环境变量覆盖。
3. 将 `build_all` 改为 per-fund 失败汇总。
4. 修复前端详情 fetch 的错误处理和竞态问题。
5. CLI 构建入口开启 logging 和进度输出。

### 中长期重构

1. 拆分 `server.py` 的 API、刷新调度和数据警报。
2. 拆分 `sources.py` 的 HTTP client、解析器和数据源适配器。
3. 拆分 `public/app.js` 的状态管理、渲染、导出和请求层。
4. 列表 API 批量预取并缓存回测摘要/诊断。
5. 引入可更新市场日历。

### 可选优化

1. 静态资源使用缓存头，API 和 HTML 保持 `no-store`。
2. `quick_start.bat` 支持 `--skip-install` 或 requirements hash。
3. 合并公告接口重复请求。
4. 同步/停服脚本增加 repo marker 和 dry-run 保护。

## 6. 测试建议

1. 新建 `tests/`，优先覆盖 `parse_navs`、`parse_purchase_limit_row`、`_market_for_symbol`、`_best_daily_price_rows`、交易日历。
2. 使用内存 SQLite 测 `estimate_intraday` 缺基金行、`calculate_backtest_row` 低覆盖、`backtest_summary` 排除低质量样本、`holdings_available_on` 的公告发布日期逻辑。
3. 增加服务端 smoke test：模拟一只基金缺数据时 `/api/funds` 仍返回 JSON。
4. 增加安全 smoke test：`POST /api/backtests/incremental` 缺 token 应被拒绝。
5. 固化 `python -m compileall` 和 `node --check public/app.js` 为最小可运行验证。

## 7. 不确定事项

1. 未运行完整联网构建，外部接口当前稳定性和全量耗时仍需实测。
2. 未逐只核验 338 只基金的人工代理规则和公告依据。
3. 未基于当前 `data/lof_inav.sqlite3` 做缓存一致性审计。
4. 未做浏览器视觉验收。
