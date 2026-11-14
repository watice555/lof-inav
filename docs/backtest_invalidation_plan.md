# 回测派生缓存失效协议实施计划

## 1. 背景与目标

当前回测失效由调用方零散维护：报告刷新通过 `pending_report_backtests`
记录季报发布日期，NAV 刷新另行传递修订日期，单基金 CLI 和直接回测路径没有
完整的 pending 清除协议。这会造成以下风险：

- 持仓或历史 NAV 已变化，但旧回测仍被当作正常结果展示；
- CLI 成功重算后 pending 无法自动清除，后续服务反复重算；
- 多个变化锚点互相覆盖，较早的失效边界可能丢失；
- 公告调度、输入变更检测和回测状态维护混在同一流程，调用方容易再次漏处理。

本阶段建立统一的派生缓存失效协议：任何会影响已持久化回测的输入变化都在同一
事务内标记 dirty；只有原子回测替换确认覆盖 dirty 范围后才能清除 dirty。

## 2. 范围

本阶段包含：

1. 用通用的 `backtest_dirty_from_by_code` 元数据取代只表达季报变化的
   `pending_report_backtests`，并兼容迁移旧值。
2. 将历史 NAV 修订、历史 NAV 补插和实际持仓快照变化纳入统一失效检测。
3. 将 dirty 的清除放进回测原子写入路径，覆盖 full、incremental 和 CLI。
4. 让 `refresh_reports` 只负责廉价候选筛选、导入调度和 dirty 消费，不再直接
   推导或写入失效锚点。
5. 更新服务端状态读取与告警，使旧数据库和新元数据均可正常工作。

本阶段不包含：行情可信度校验、前复权缓存代际、回测展示阈值、2027 日历正式
公告确认和全量导入性能优化。

## 3. 核心不变量

### 3.1 输入侧

- dirty 锚点表示：该日期起可能存在与当前输入不一致的已持久化回测。
- 新变化只能创建 dirty，或把已有锚点移动到更早日期：

  ```text
  dirty[code] = min(existing_dirty, new_dirty)
  ```

- 快照未变化的重导入不得清除、覆盖或后移已有 dirty。
- dirty 标记和受影响旧回测的删除必须与输入写入处于同一事务/savepoint；输入
  写入失败时二者一起回滚。

### 3.2 回测侧

- 只有回测原子替换路径能够清除 dirty。
- “没有抛异常”或“返回非空 rows”不能单独代表已覆盖；回测路径必须掌握
  `full_replace`、`processed_from` 和 `processed_through`。
- full replacement 会删除该基金全部旧回测。因此 dirty 早于本次保留窗口并不
  会留下静默过期段；只要处理范围已到达 dirty，便可清除。
- incremental replacement 只有在替换起点不晚于 dirty，且处理范围已到达 dirty
  后才能清除。
- dirty 晚于最新已处理 NAV（例如报告发布日期尚未进入 NAV 序列）时必须保留。
- 缺价导致的回测缺失与“旧行已过期”是不同状态；旧行删除后可以解除 dirty，
  缺失价格继续由现有 unresolved/diagnostic 元数据报告。

## 4. 元数据与兼容迁移

新增模块 `app/backtest_state.py`，提供唯一状态 API：

```python
load_backtest_dirty(con) -> dict[str, str]
mark_backtest_dirty(con, code, from_date) -> str
invalidate_backtests(con, code, from_date) -> str
resolve_backtest_dirty(
    con,
    code,
    *,
    full_replace: bool,
    processed_from: str | None,
    processed_through: str | None,
) -> bool
```

约束：

- 所有日期均严格校验为 ISO `YYYY-MM-DD`。
- `load_backtest_dirty` 合并新键和旧键 `pending_report_backtests`；同一基金取较早
  锚点。
- 首次写入新状态时删除旧 metadata 键，完成惰性迁移。
- API 不自行 commit，事务边界由调用者控制。
- `invalidate_backtests` 在同一事务内先合并 dirty，再删除 `date >= dirty` 的旧行。
- 发布本协议时提升 `BACKTEST_CACHE_VERSION`，一次性清空协议上线前可能已由
  `--current-only` 等路径遗留、但没有 legacy pending 标记的旧回测；正常增量流程
  随后重建。仅迁移 metadata 无法识别这种历史静默错误。

## 5. 输入变化检测

### 5.1 NAV

扩展 `upsert_nav_rows` 的结果：

- 已存在日期的 NAV、distribution 或 return_pct 改变：失效候选为该日期；
- 新插入日期不晚于写入前的最新 NAV：属于历史补插，失效候选为该日期；
- 只在原最新 NAV 之后正常追加：不产生历史 dirty，交给普通增量回测处理；
- 返回 `earliest_invalidating_date`，并在存在候选时调用统一失效 API。

首次导入没有旧 NAV/旧回测，不产生无意义 dirty。

### 5.2 持仓

在 `import_fund_data` 的 `replace_fund_snapshot` savepoint 内：

1. 删除旧持仓前捕获规范化旧快照；
2. 写入并验证新持仓；
3. 捕获规范化新快照；
4. 对当前已持久化 backtest 的 `previous_date`，分别按旧、新快照模拟
   `holdings_available_on` 的选择规则；
5. 若签名不同，以最早受影响 backtest 的 `date` 作为 dirty 锚点。

持仓签名包含并排序：

```text
report_date, publish_date, secid, weight, source
```

权重使用稳定的规范化精度，避免浮点尾差触发无谓重算。`symbol` 和 `name` 不参与
估值，不作为 dirty 条件。

选择该精确行为：回填更旧季报若不影响任何当前持久化回测，则不产生 dirty；以后
扩大回测窗口时会直接使用新快照计算，不存在旧派生行需要修复。

公告 ID/title/publish_date 的对比继续作为 `refresh_reports` 的廉价导入候选筛选；
是否失效回测只由实际 NAV/持仓输入差异决定。相同公告元数据下的源端更正由后续
低频深度快照复核解决，不在本阶段把每日报告检查扩大为 338 只完整导入。

## 6. 回测覆盖与 dirty 解除

`run_backtest` 和 `run_backtest_incremental` 在调用
`replace_backtest_rows_atomic` 时传入处理覆盖信息，并在同一个 savepoint 中调用
`resolve_backtest_dirty`。

### 6.1 Full

- `full_replace=True`；
- `processed_through` 使用本次考虑到的最新 NAV 日期，而不是最后一条成功写入的
  backtest 日期；
- 由于旧回测被全部删除，只要 `processed_through >= dirty` 即可清除；
- 如果 dirty 在未来，保留。

### 6.2 Incremental

- `full_replace=False`；
- `processed_from` 为实际删除/重算起点；
- 仅当 `processed_from <= dirty <= processed_through` 时清除；
- 少于两条 NAV、没有可用持仓或没有可写结果时，只要受影响旧行已按起点原子删除，
  仍按处理覆盖解除 dirty；数据缺失通过现有诊断继续暴露。

## 7. 调度重构

- 删除 `import_fund_data(..., pending_backtest_from=...)` 参数。
- `refresh_reports` 保留公告三元组/修复检查，用于筛选需要深度导入的基金；不再
  写入本地 pending 字典。
- 所有导入完成后，`refresh_reports` 重新调用 `load_backtest_dirty`，对 dirty 基金
  调用增量回测。
- `refresh_incremental_backtests` 将 dirty 锚点与正常七日 overlap 取较早值。
- `refresh_navs` 不再维护独立的 NAV revision 触发源；统一读取 dirty 状态。
- 服务端数据警报和状态接口改读新 API，对用户仍显示“回测将从某日重算”。

这里实行“失效触发源单一化”，但不移除 `refresh_reports` 的廉价网络调度筛选；
调度候选与派生缓存失效是两层不同职责。

## 8. 测试矩阵

### 状态层

- 旧 metadata 可读取并迁移；新旧同时存在时取较早日期。
- 协议上线前的回测缓存版本会被一次性清空，当前版本不重复清空。
- 多次 mark 只前移、不后移。
- 无效日期被拒绝。
- invalidate 同事务删除受影响行，rollback 可恢复。
- 未覆盖 dirty 时 resolve 保留；full/incremental 覆盖后清除。

### NAV

- 已有 NAV 数值修订从最早修订日失效。
- 历史缺失 NAV 补插产生 dirty。
- 独立调用 NAV upsert 后仍由调用者决定 commit/rollback，内部 savepoint 不得隐式
  提交。
- 正常追加最新 NAV 不产生历史 dirty。
- 多个 NAV/持仓变化合并为最早锚点。

### 持仓

- 实际持仓变化产生 dirty，公告变化但估值输入相同不产生 dirty。
- `publish_date`、secid、weight、source 变化均被检测。
- 更旧季报回填不影响现有 backtest 时不产生 dirty。
- 无变化重导入保留既有 dirty。
- 快照导入失败时旧持仓、旧回测和 dirty 一起回滚。

### 回测与调度

- CLI full 回测覆盖 dirty 后自动清除。
- full 回测窗口起点晚于 dirty 仍可清除，因为旧缓存被全量替换。
- dirty 晚于最新 NAV 时保留。
- incremental 起点晚于 dirty 时保留；完整覆盖后清除。
- 回测异常时 dirty 保留。
- `refresh_reports` 不直接写 dirty，但能消费导入产生的 dirty。
- pending 重试、backtests-disabled 和部分失败语义保持明确。

## 9. 并行实施分工

1. **状态层**：新增 `app/backtest_state.py` 和状态层单元测试，完成兼容迁移、
   mark/invalidate/resolve API。
2. **回测写入层**：修改 `app/valuation.py`，把覆盖信息和 dirty 解除纳入 full/
   incremental 原子替换路径，补回测边界测试。
3. **输入与调度层**：修改 `app/build.py`、`app/server.py` 及相关测试，实现 NAV/
   持仓实际差异检测、单一失效触发和调度消费。
4. **主代理集成**：解决接口衔接、迁移旧调用、补遗漏测试，运行全量验证并创建
   聚焦提交。

## 10. 验证与完成标准

按顺序执行：

```bash
python -m unittest tests.test_backtest_dirty_state
python -m unittest tests.test_fund_snapshot tests.test_nav_refresh tests.test_report_refresh tests.test_backtest_cache
python -m unittest discover -s tests -p 'test_*.py'
python scripts/validate_config.py
python -m compileall -q app scripts *.py
node --check public/app.js
git diff --check
```

完成标准：全部离线测试通过；旧 metadata 可无损读取；没有调用方直接写旧 pending
键；输入变化后不存在未标记的旧回测；未覆盖 dirty 的回测不会错误清除状态；工作区
只包含本阶段相关源码、测试、文档与实施日志。
