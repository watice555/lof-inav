# LOF iNAV

跨市场 LOF 日内估值实验台。项目把基金净值、公告持仓、代理资产、场内行情、汇率、申购状态和历史回测统一写入本地 SQLite 缓存，并提供一个静态网页查看估算 iNAV、折溢价、持仓代理和回测误差。

项目核心不是直接用最新公布净值和场内价格比较，而是根据基金已披露持仓、人工代理规则和实时市场行情估算当前 iNAV，再用场内交易价格相对估算 iNAV 计算折溢价。已公布净值天然存在滞后，QDII、港股、美股、商品、债券和其他跨市场 LOF 尤其明显；这些基金在境外市场交易时段、汇率和基金净值披露节奏上都可能和场内 LOF 交易价格错位。

对于非 A 股基金，项目逐只维护人工持仓代理规则：根据定期报告、底层指数、ETF 篮子、商品或债券敞口，把公告持仓转换为可跟踪的等效资产，并用回测 MAE、MAE/波动、最新误差、最大误差和覆盖仓位持续修订规则。

这个仓库适合两类使用方式：

- 直接运行本地网站，观察已收录 LOF 的估算 iNAV、交易价格和折溢价。
- 为新的 LOF 增加持仓代理规则，导入数据并用回测判断规则是否可靠。

License: MIT

## 免责声明

本项目仅为个人学习、研究和技术验证用途，所有数据、规则、估值结果和回测结果均基于公开信息、第三方行情接口、本地缓存以及自定义代理逻辑自动或人工整理生成。项目不保证数据来源稳定，也不保证任何展示结果的准确性、完整性、及时性或可用性。

页面和接口展示的系统估值、折溢价率、持仓代理、申购状态、回测误差等，均不代表基金实时净值、基金管理人公告数据、交易所官方 IOPV 或未来表现。本项目不构成任何投资建议、收益承诺、估值结论、交易信号，也不应作为买卖、申购、赎回任何基金份额或其他金融产品的依据。

基金投资存在风险。任何投资判断都应以基金合同、招募说明书、定期报告、基金管理人公告、交易所公告等正式文件为准，并由使用者自行核验、自行决策和自行承担全部风险。

## 当前覆盖状态

已导入和待导入情况以 [`docs/lof_universe_gap.csv`](docs/lof_universe_gap.csv) 为准。它是交易所 LOF 全量清单和网站收录状态的基准文件；[`config/fund_rules.json`](config/fund_rules.json) 是运行时规则配置，不再在 README 中维护逐只基金清单。

关键字段：

- `included_in_site`：是否已经进入网站和 `config/fund_rules.json`。`yes` 表示已收录，`no` 表示未收录。
- `market_category`：主分类，例如 `A股-指数`、`QDII-港股`、`商品-原油`、`FOF`。
- `site_type`：前端展示用的简化分组。
- `add_priority`：新增优先级；`already_in_site` 表示已收录，其他值表示待补批次。
- `eastmoney_name`：和东方财富基金页匹配到的名称。

当前 CSV 快照见 [`docs/lof_gap_analysis.md`](docs/lof_gap_analysis.md)：交易所 LOF 合计 404 只，网站已收录 338 只，尚未收录 66 只。查看最新统计可以运行：

```powershell
@'
import csv
from collections import Counter

rows = list(csv.DictReader(open("docs/lof_universe_gap.csv", encoding="utf-8-sig")))
print("total", len(rows))
print("included", sum(r["included_in_site"] == "yes" for r in rows))
print("pending", sum(r["included_in_site"] != "yes" for r in rows))
print(Counter(r["market_category"] for r in rows if r["included_in_site"] == "yes"))
'@ | python -
```

新增或移除基金时，需要同时保持两处一致：

1. `config/fund_rules.json` 决定程序实际导入哪些基金。
2. `docs/lof_universe_gap.csv` 决定项目文档和覆盖状态如何统计。

## 快速开始

本项目是普通 Python 项目，主要在 Windows + PowerShell 下使用。首次运行建议先创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

首次使用需要构建本地缓存：

```powershell
python build.py
```

`build.py` 会导入 `config/fund_rules.json` 中的全部基金，刷新净值、公告、持仓/代理、行情、申购限额、日线价格和最近 180 条净值回测。基金数量较多，首次运行会比较慢，并且需要访问东方财富、新浪财经和 Yahoo Finance。

构建完成后启动本地网站：

```powershell
python serve.py
```

浏览器打开：

```text
http://127.0.0.1:8000
```

也可以在依赖已安装后运行 [`start.bat`](start.bat)，它会启动 `serve.py` 并打开浏览器。注意：`start.bat` 不会替你执行首次全量构建。

## 日常命令

全量刷新所有已配置基金：

```powershell
python build.py
```

刷新单只基金，适合修改规则后验证：

```powershell
python import_fund.py 160924
```

可选参数：

```powershell
python import_fund.py 160924 --days 180 --outliers 5
```

单只导入会刷新该基金的净值、公告、持仓/代理、相关行情、申购限额、日线价格和回测，并输出：

- 基金名称和规则备注。
- 回测 MAE、波动、最大误差和最新误差。
- 最新持仓或代理资产。
- 误差最大的几个净值日期。

只刷新最新公告链接：

```powershell
python refresh_announcements.py
```

如果修改了 `config/fund_rules.json`，需要重启 `serve.py`，因为基金规则在 Python 模块导入时加载。

## 数据流程

运行时自动抓取的数据源主要在 [`app/sources.py`](app/sources.py) 中实现：

- 东方财富基金页 `pingzhongdata/{code}.js`：基金名称、历史单位净值、分红、资产配置和平台返回的部分持仓代码。
- 东方财富 F10 `FundArchivesDatas.aspx`：季报股票持仓和占净值比例。它是结构化入口和 fallback，不等同于原始公告事实源。
- 东方财富公告接口 `api.fund.eastmoney.com/f10/JJGG`：定期报告标题、公告发布日期、公告 ID 和公告页链接。
- 东方财富基金申购状态页 `Data/Fund_JJJZ_Data.aspx?t=8`：场外申购/赎回状态、最小申购金额、日累计申购限额和数据日期。
- 东方财富行情接口 `push2` / `push2his`：场内 LOF、A 股/港股/美股代理、指数、汇率等实时行情和历史日线；部分指数实时行情会走 `stock/get` 或 `trends2/get`。
- 东方财富期货接口 `futsseapi.eastmoney.com`：部分期货代理的实时行情，例如沪银主连。
- 新浪财经 `hq.sinajs.cn`、`quotes.sina.cn`、`stock2.finance.sina.com.cn`：A 股实时和日线 fallback、港股指数/美股/外汇/全球期货实时 fallback，以及部分期货日线。
- Yahoo Finance chart API：配置在 `YAHOO_PRICE_SYMBOLS` 中的美股、港股、ETF、指数和商品代理的实时/日线；配置在 `US_EQUITY_CLOSE_MARKS` 中的 WTI、Brent、黄金、白银等回测收盘标记价。

人工维护或离线核验的数据源：

- 原始定期报告 PDF/公告页：QDII、FOF、ETF 联接、商品类基金和复杂主动基金的最高优先级事实源。核验材料保存在 `data/`，等效持仓写入 `config/fund_rules.json` 的 `manual_holdings`。
- [`docs/lof_universe_gap.csv`](docs/lof_universe_gap.csv)：全量 LOF 清单和收录状态基准。其来源和统计口径见 [`docs/lof_gap_analysis.md`](docs/lof_gap_analysis.md)，不是运行时自动抓取的一部分。

本地缓存写入 `data/lof_inav.sqlite3`。原始公告核验材料保留在 `data/` 的 `.pdf`、`.txt`、`.html` 文件中。

数据库主要表：

- `funds`：已配置基金基础信息。
- `navs`：历史单位净值、分红和净值涨跌。
- `holdings`：每期持仓、人工穿透资产或代理资产。
- `quotes`：实时行情。
- `daily_prices`：历史日线。
- `mark_prices`：用于回测的 Yahoo 日线收盘标记价。
- `backtests`：相邻净值日回测结果。
- `fund_announcements`：最新公告入口。
- `fund_purchase_limits`：申购、赎回状态和限额。

## 估值逻辑

折溢价计算以系统估算 iNAV 为基准，而不是以最近一次公布净值为基准。公布净值用于提供估值起点和回测校验；日内估值会把最新净值之后的持仓资产涨跌、汇率变化和代理资产变化叠加进去。

日内估值：

```text
资产人民币收益 = (1 + 资产本币涨跌幅) * (1 + 汇率涨跌幅) - 1
估值 = 最新净值 * (1 + sum(资产权重 * 资产人民币收益))
折溢价率 = 场内交易价格 / 估值 - 1
```

如果标的行情时间不晚于最新净值日，例如港股周末休市且净值已更新到周五，该标的实时涨跌按 0 处理，避免重复叠加同一天市场涨跌。外币资产会按资产所属市场叠加人民币汇率变化：港股和港股指数主要使用 `HKDCNYC`，美股、美债、美元商品、美元 ETF、美元指数代理主要使用 `USDCNYC`。

回测：

```text
资产人民币收益 = (1 + 资产两个净值日之间涨跌) * (1 + 汇率两个净值日之间涨跌) - 1
下一净值日估值 = 前一净值日净值 * (1 + sum(资产权重 * 资产人民币收益))
误差 = 下一净值日估值 / 下一净值日公布净值 - 1
```

现金分红日的实际值按 `当日单位净值 + 当日每份现金分红` 计算，避免除息造成假误差。回测只使用当时已经公告的持仓：

```text
净值日可用持仓 = publish_date <= 净值日 的最新一期持仓
```

商品和贵金属使用双口径：实时估值使用连续交易的当前行情；历史回测优先使用 Yahoo 日线收盘标记价，减少小时线异常点对误差评估的影响。`mark_prices` 只用于回测，实时估值基准价必须与实时行情使用同一 `secid` 和同一价格尺度。

## 回测指标

回测按相邻两个净值日逐行计算。每行的 `error_pct` 是估算净值相对公布净值的偏差：

```text
error_pct = estimated_nav / actual_nav - 1
```

其中 `actual_nav` 已包含当日每份现金分红。`covered_weight` 是当行回测实际参与计算的持仓权重合计；缺少日线、汇率或无法取价的资产不会计入覆盖仓位，也不会贡献收益。

列表页的回测摘要使用最近最多 120 条回测记录：

- `回测 MAE`：`abs(error_pct)` 的平均值，即平均绝对误差。
- `MAE/波动`：`回测 MAE / 净值日收益率标准差`，用于把误差放到基金自身波动里比较。
- `覆盖仓位`：日内估值里当前最新持仓/代理资产有实时行情并参与计算的权重合计。
- `最大误差`、`最新误差`、`平均覆盖仓位`：用于判断代理规则是否稳定。

单只基金详情页展示最近 60 条回测明细，包括日期、实际净值、估算净值、单日误差、覆盖仓位、历史场内收盘价和历史折溢价。

## 规则配置

基金级规则集中在 [`config/fund_rules.json`](config/fund_rules.json)。常见字段：

- `exchange_market`：场内基金交易市场编号，深市为 `0`，沪市为 `1`。
- `type`：前端类型筛选和列表展示。
- `proxy_secids` / `proxy_basis` / `proxy_weight`：按非现金仓位、股票仓位缺口或固定权重生成代理资产。
- `manual_holdings`：人工核验后的公告持仓、指数代理、商品穿透、ETF 篮子或底层指数篮子。
- `manual_holdings_mode`：控制平台持仓和人工规则的组合方式。
- `note`：前端展示的规则说明。

`manual_holdings_mode` 当前支持：

- `overlay`：在平台结构化持仓基础上叠加人工规则。
- `replace`：完全使用公告核验后的人工等效敞口。
- `proxy_only`：只按资产配置仓位映射到指数、ETF 或商品代理。
- `proxy_then_manual_replace`：历史阶段先用代理，已核验公告期用人工持仓替换。

配置中允许保留 `rationale`，记录代理依据、忽略项和权重折算逻辑。代理原则见 [`docs/lof_proxy_rules.md`](docs/lof_proxy_rules.md)，逐只导入流程见 [`docs/agent_import_guide.md`](docs/agent_import_guide.md)。

非 A 股基金的人工规则通常需要逐只维护：先根据公告持仓、指数编制、ETF 持仓、商品合约或债券久期建立可交易代理，再用回测误差和覆盖仓位检查代理是否偏离基金实际净值表现。误差异常时，应回到公告和代理规则中核对权重、汇率口径、标的选择和缺失行情。

新增基金的基本流程：

1. 在 `docs/lof_universe_gap.csv` 中找到目标基金，确认它是有场内交易价格的 LOF。
2. 在 `config/fund_rules.json` 增加最小规则，至少包含 `exchange_market`、`type` 和代理规则。
3. 对 QDII、FOF、ETF 联接、商品基金，下载并核验最近定期报告，不要只依赖平台持仓页。
4. 运行 `python import_fund.py <code>`。
5. 检查输出中的最新持仓、覆盖仓位、缺失行情和最大误差日期。
6. 打开网站检查列表、持仓详情和回测详情。
7. 如果决定纳入网站，把 CSV 中该基金的 `included_in_site` 改为 `yes`，`add_priority` 改为 `already_in_site`。

## 前端与接口

前端位于 [`public/`](public/)，服务端由 [`app/server.py`](app/server.py) 提供接口。

- `GET /api/funds`：基金列表、估值、折溢价、公告、申购限额、回测摘要。
- `GET /api/funds/{code}/holdings`：最新一期持仓/代理资产及对应行情。
- `GET /api/funds/{code}/backtest`：最近 60 条回测明细。

页面功能：

- 顶部状态栏：展示净值和行情最近刷新时间；`刷新` 按钮手动调用 `/api/funds`；`导出CSV` 和 `导出PNG` 导出当前筛选后的列表。
- 数据警报：当基金缺少实时行情、缺少回测，或最新回测存在价格回退、汇率回退、数据缺失时显示警报；点击警报会打开对应基金详情。
- 类型筛选：按 `type` 分组生成复选项，显示每类数量，支持全选和全不选。
- 申购状态筛选：可显示全部基金，也可屏蔽 `display` 为 `暂停` 的基金。
- 折溢价筛选：可输入溢价和折价阈值，筛出 `premium > 阈值` 或 `premium < -阈值` 的基金。
- 主列表：展示基金名称、代码、净值日期、类型、公告、申购限额、上一日净值、场内价格、系统估值、折溢价率、覆盖仓位、回测 MAE、MAE/波动和规则备注。
- 列表排序：点击表头可在升序、降序、默认顺序之间切换；支持基金、类型、公告日期、申购限额、净值、价格、估值、折溢价、覆盖仓位和回测指标排序。其中基金列实际按基金代码大小排序。
- 公告入口：公告列链接到东方财富公告页，并按公告 ID 拼出 PDF 入口。
- 持仓/代理详情：点击基金行后展示最新一期持仓或代理资产，包括资产名称、`secid`、权重、最新价格、价格时间和来源标记。
- 最近回测详情：展示最近回测行，包括实际净值、场内收盘价、历史折溢价、估算净值、误差、覆盖仓位和数据质量提示。
- 导出口径：CSV 和 PNG 都使用当前筛选、排序后的可见基金；PNG 会包含当前筛选摘要、刷新时间和免责声明。
- 自动刷新：页面加载后立即拉取一次数据，之后每 60 秒自动刷新列表行情和状态。

服务端刷新策略：

- 净值缓存为空时同步补齐；之后 `/api/funds` 最多每 15 分钟触发一次后台净值刷新和增量回测。
- 实时行情缓存为空时同步补齐；之后最多每 60 秒后台刷新一次。
- 申购限额缓存为空时同步补齐；之后最多每 1 小时后台刷新一次。
- 列表接口只刷新当前基金场内价格、汇率和最新持仓/代理标的行情；历史持仓标的日线由 `python build.py` 或单只导入增量更新。

## 当前限制

- 这是估值实验工具，不是交易建议；代理规则需要随基金公告和标的变化持续核验。
- 未覆盖现金、缺失资产和无法取价资产默认贡献 0。
- ETF 联接、FOF、商品和跨市场指数基金优先使用公告穿透或指数代理，准确度取决于人工规则质量。
- 新增基金应先跑 `python import_fund.py <code>`，检查覆盖仓位、最大误差日期和最新持仓，再纳入日常全量构建。
- 全量覆盖状态不要从 README 的文字推断，始终以 `docs/lof_universe_gap.csv` 的 `included_in_site` 字段为准。

## 目录结构

```text
app/
  build.py       数据导入、行情刷新、日线刷新、回测调度
  config.py      配置加载、汇率映射、Yahoo 标记价映射
  db.py          SQLite schema 和元数据读写
  server.py      静态服务与 JSON API
  sources.py     东方财富、新浪财经、Yahoo 数据源请求和解析
  valuation.py   日内估值、汇率折算、回测
config/
  fund_rules.json
data/
  lof_inav.sqlite3         本地缓存，运行构建后生成
  *.pdf / *.txt / *.html   原始公告核验材料
docs/
  agent_import_guide.md    新 LOF 导入执行手册
  lof_gap_analysis.md      全量清单统计和收录缺口
  lof_universe_gap.csv     LOF 全量清单和收录状态基准
  lof_proxy_rules.md       代理规则和分类经验
public/
  前端页面、样式和交互脚本
```

## 致谢

感谢“小鱼的储钱罐”老师对我的 LOF 套利启蒙，以及在估值思路上的启发。

感谢 OpenAI Codex，让我能够把一些原本停留在想法和表格里的东西，逐步落实成可以运行、验证和迭代的代码。

感谢东方财富、新浪财经、Yahoo Finance 等公开数据源。本项目的数据抓取、行情补齐和回测验证都离不开这些公开信息。
