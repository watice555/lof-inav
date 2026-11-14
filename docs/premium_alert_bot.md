# 交易日智能折溢价 Bot 报告

[`scripts/report_premium_alerts.py`](../scripts/report_premium_alerts.py) 用于在中国交易日强制刷新本地 LOF iNAV 数据，等待净值和行情任务完成，然后按页面的智能折溢价信号报告卖出溢价和买入折价。默认阈值为 1.5%；暂停申购筛选与页面“屏蔽暂停申购”完全相同，只排除申购状态 `display` 为“暂停”的基金。

## 运行逻辑

脚本每次运行会：

1. 使用项目内交易日历判断当天是否为中国交易日，非交易日正常退出且不发送消息。
2. 从 `data/lof_inav.pid` 发现本地服务实际端口，也可通过 `LOF_INAV_BASE_URL` 指定。
3. 获取本地 CSRF token，调用 `POST /api/refresh/current` 强制刷新净值和当前估值所需全部行情。
4. 等待两类刷新完成，并验证完成时间或刷新进度；默认最长等待 30 分钟。
5. 通过 `/api/funds?refresh=0` 读取快照而不再调度后台任务。默认筛选 `sell_premium` 信号且 `smart_premium > 0.015`，或 `buy_discount` 信号且 `smart_premium < -0.015`。报告分别展示买一价和卖一价；没有有效信号时不会回退到成交折溢价。排除暂停申购的基金，限购或申购状态缺失的基金仍保留。
6. 排除本轮净值刷新失败的基金，在报告中标记行情缺失、实时估值降级和低于 60% 的取价覆盖。
7. 即使没有基金命中阈值，也发送“未发现”，从而证明当天任务已经执行。

这是项目的指示性估值，不是官方净值或交易建议。报告保留同样的提示。

## Bot 配置

凭据只从环境变量读取，不要把真实 Webhook、Token 或 Chat ID 写入仓库、脚本、文档、日志或提交。

| 环境变量 | 必需 | 说明 |
| --- | --- | --- |
| `LOF_INAV_BOT_TYPE` | 否 | `generic`、`slack`、`feishu`、`wecom`、`dingtalk` 或 `telegram`；默认 `generic` |
| `LOF_INAV_BOT_WEBHOOK_URL` | 发送时必需 | Bot Webhook；Telegram 使用完整的 `sendMessage` API 地址 |
| `LOF_INAV_BOT_CHAT_ID` | 仅 Telegram | 接收消息的 Chat ID |
| `LOF_INAV_BASE_URL` | 否 | 例如 `http://127.0.0.1:8001`；不设置时读取 PID 文件 |

支持的载荷格式：

- `generic`、`slack`：`{"text": "..."}`。
- `feishu`：飞书自定义机器人文本消息。
- `wecom`：企业微信群机器人文本消息。
- `dingtalk`：钉钉自定义机器人文本消息。
- `telegram`：Bot API `sendMessage`，还需设置 Chat ID。

如果其他 Bot 接受不同 JSON 结构，应在 [`app/premium_alerts.py`](../app/premium_alerts.py) 增加明确的适配器，不要在定时命令中拼接不受验证的请求。

## 试运行

先启动本地服务：

```bash
./start.command
```

强制刷新、生成报告但不发送：

```bash
.venv/bin/python scripts/report_premium_alerts.py --dry-run --force-run
```

`--force-run` 只用于测试；正式定时任务不要添加，否则节假日也会运行。完成 Bot 环境变量配置后，手工发送一次：

```bash
.venv/bin/python scripts/report_premium_alerts.py --force-run
```

常用选项：

```text
--threshold-percent 1.5  智能折溢价阈值，单位为百分数
--refresh-timeout 1800   刷新最长等待秒数
--poll-seconds 2         状态轮询间隔
--min-priced-ratio 0.6   低取价覆盖提示阈值
```

服务未运行、刷新超时或刷新任务整体失败时，脚本返回非零状态；如果 Bot 已配置，还会尝试发送失败通知，但不会发送可能陈旧的阈值判断。

超时通知包含实际等待秒数、等待上限，以及净值和行情各自的状态、最近阶段和已处理/总数（服务提供进度时）。若尚未启动，会区分任务占用和等待已有行情刷新结束。通知不包含上游原始错误或凭据。

## macOS 定时运行（推荐）

[`scripts/install_macos_premium_alert.py`](../scripts/install_macos_premium_alert.py) 会安装用户级 `launchd` 任务 `local.lof-inav.premium-alert`，每周一至周五本机时间 14:30 触发。它不调用 Codex，不消耗 Codex 额度，也不要求 Codex App 或 LOF 网页保持打开。运行包装会先核验中国交易日；交易日才从 macOS 钥匙串读取凭据、检查本地 LOF 服务，并在服务不存在时临时启动，任务完成后只停止本次启动的服务。

安装或更新任务：

```bash
cd /absolute/path/to/LOF_iNAV
.venv/bin/python scripts/install_macos_premium_alert.py
```

安装后可立即真实运行一次测试：

```bash
launchctl kickstart -k gui/$(id -u)/local.lof-inav.premium-alert
```

查看状态和脱敏运行日志：

```bash
launchctl print gui/$(id -u)/local.lof-inav.premium-alert
tail -n 100 data/premium_alert_launchd.log data/premium_alert_launchd.error.log
```

卸载任务：

```bash
.venv/bin/python scripts/install_macos_premium_alert.py --uninstall
```

LaunchAgent 文件中只有项目路径和执行时间，不保存 Token、Chat ID 或 Telegram URL。macOS 用户需要保持登录；电脑在触发时睡眠的情况下，`launchd` 通常会在唤醒后补跑日历任务。

## Linux 与 Windows 定时运行

Linux 可使用 cron。若希望取得 A 股收盘价，可在每个工作日北京时间 15:05 触发；脚本自身会再次核验中国交易日，所以周末和已维护的节假日不会发送。cron 的任务主体为：

```cron
5 15 * * 1-5 cd /absolute/path/to/LOF_iNAV && .venv/bin/python scripts/report_premium_alerts.py >> data/premium_alerts.log 2>&1
```

Windows 任务计划程序配置为：

- 触发器：每周一至周五 15:05。
- 程序：项目 `.venv\Scripts\python.exe` 的绝对路径。
- 参数：`scripts\report_premium_alerts.py`。
- 起始位置：项目根目录的绝对路径。
- 失败时重试：建议间隔 5 分钟、最多 2 次。

定时器必须使用能够读取 Bot 环境变量的用户上下文。优先使用操作系统的凭据存储或调度平台的 Secret 功能；不要把真实 Webhook 直接写进 crontab、plist、任务参数或仓库内的包装脚本。

本项目的中国节假日表目前维护到 2027 年。2024—2026 年采用[上交所年度休市公告](https://www.sse.com.cn/disclosure/dealinstruc/closed/list/)；由于上交所尚未发布 2027 年年度休市通知，2027 年暂按[国务院现行调休规则](https://www.gov.cn/zhengce/content/202411/content_6986382.htm)推算，定时任务运行时会在日志中提示该年份仍待复核。推算口径为春节自除夕起 8 天、劳动节 5 天、国庆节 7 天，元旦、清明、端午和中秋连休或调休 3 天且逢周三仅当日放假；待正式公告发布后应及时核对更新。进入 2028 年等未维护年份后，任务会停止并报错，避免把普通工作日误判成交易日；只有明确接受“周一至周五”退化规则时才使用 `--allow-unknown-calendar`。
