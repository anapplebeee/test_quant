# Codex 会话进度快照（2026-08-31）

> 记录本轮 Codex 开发的实际完成进度与遗留事项，作为下次继续的交接依据。
> 验证方式：定向测试 8 个文件共 66 项全部通过（`66 passed in 6.23s`）；
> 全量测试与静态检查尚未运行。

## 一、已完成（代码与测试均已落地）

### P0 前后端账户与交易闭环

- [x] `api/manual_trading_api.py`（471 行）：隔离 Gradio 与 SQLite 领域仓库，
      提供计划审批/取消/调减、按交易日过期（`expire_plans`）、成交录入、
      成交自动匹配计划订单（`match_planned_order`）、人工下单 CSV 导出、
      对账快照导入与差异确认；
- [x] `frontend/pages/manual_trading.py`：手动交易前端页，已在 `app.py` 注册；
- [x] 风险管理页与策略监控页统一读取 SQLite 账本
      （`repository().account_state()`，消除 holdings.json 口径漂移）；
- [x] 计划调减、取消、按交易日过期、人工下单 CSV 导出；
- [x] 成交回填自动匹配计划订单（按代码/方向/日期），未匹配记为计划外成交；
- [x] 生成计划时按 `sellable_positions` 限制可卖数量
      （`quart/execution/order_generator.py` 截断 + `quart/pipeline.py` 传入账本可卖状态）。

### P0 命令操作前端化

- [x] `frontend/pages/operations.py`（222 行）：命令操作前端页，已在 `app.py` 注册；
- [x] 任务 API 安全扩展（`api/task_api.py`，配套 `tests/test_task_api_safety.py` 通过）。

### P1 策略与风险模型升级

- [x] 默认策略从已证伪的 `momentum_rotation` 切换为 `lowvol_indz`，
      新增 `live_allowlist` 准入名单（`config/settings.yaml`）；
- [x] 修复配置优先级：`resolve_params` 现为 `全局参数 < strategy.overrides < 显式传入`，
      旧实现会漏读全局参数（`quart/strategy/__init__.py`，
      配套 `tests/test_param_precedence.py` 通过）；
- [x] 低波策略参数调整：`lowvol_indz` rebalance_days=45 / top_k=30 / rank_buffer=0.5。

### 新增基础模块（超出原计划，属于阶段 D/F 提前落地）

- [x] `quart/data/calendar.py` + `scripts/update_trading_calendar.py`：
      A 股交易日历缓存（CSV 落盘 `data/meta/trading_calendar.csv`，
      缓存缺失时退化为工作日规则）；
- [x] `quart/broker/`（base/models/paper）：`BrokerAdapter` 抽象 + 订单状态机
      + `PaperBrokerAdapter` 内存模拟盘，为券商 API 接入做准备；
- [x] 新增测试：`test_paper_broker.py`、`test_trading_calendar.py`、
      `test_manual_trading_api.py`，并扩充 `test_manual_trading.py`、
      `test_pipeline_smoke.py`、`test_frontend_data.py`。

## 二、未完成事项（下次继续的起点）

### 阻断与风控（优先级最高）

- [ ] 未完成收盘对账时**阻断**新的正式计划审批——当前仅在前端提示"未对账"，
      `approve_plan_action` 未做强制拦截；
- [ ] 计划外交易（`MANUAL_EXTERNAL`）与端到端每日流水线测试；
- [ ] 交易日历法定节假日专项测试（当前日历缺失时静默退化工作日规则，存在误判风险）。

### 前端与文档

- [ ] 执行偏差统计（计划价 vs 成交价、计划量 vs 成交量）尚未完整输出；
- [ ] 前端明确区分"研究/观察/候选/准入"策略状态（`live_allowlist` 已建，页面未展示）；
- [ ] README 重构已改动 83 行，需对照 FRONTEND_STRATEGY_DEVELOPMENT_PLAN.md
      第 2 节 P1 文档项核对是否覆盖"安装→启动→初始化→每日操作→验证→排查"全链路；
- [ ] 两份规划文档（`FRONTEND_STRATEGY_DEVELOPMENT_PLAN.md`、
      `MANUAL_TRADING_T1_SYNC_PLAN.md`）中的勾选项与实际代码未同步，
      完成本轮后需按实际进度更新。

### 券商对接（阶段 F 剩余）

- [ ] `BrokerAdapter` 回报统一写入 `FillService`（当前 PaperBroker 独立内存态）；
- [ ] API 订单状态机与计划/成交链路打通；
- [ ] 各券商 CSV/XLSX 列映射 profiles（`broker_profiles`）；
- [ ] 全量测试 + 静态检查（ruff/类型检查）运行并记录遗留失败。

## 三、验证记录

```text
.venv/Scripts/python.exe -m pytest \
  tests/test_paper_broker.py tests/test_trading_calendar.py \
  tests/test_manual_trading_api.py tests/test_param_precedence.py \
  tests/test_manual_trading.py tests/test_pipeline_smoke.py \
  tests/test_task_api_safety.py tests/test_frontend_data.py -q
=> 66 passed in 6.23s
```

## 四、改动清单（本次待提交）

- 修改 23 个文件（+666/-200）：pipeline、order_generator、strategy 注册、
  config、前端 4 页、手动交易 repository/io、脚本入口、8 个测试文件；
- 新增 11 个路径：`api/manual_trading_api.py`、`frontend/pages/manual_trading.py`、
  `frontend/pages/operations.py`、`quart/broker/`（4 文件）、
  `quart/data/calendar.py`、`scripts/update_trading_calendar.py`、
  `tests/test_manual_trading_api.py`、`tests/test_paper_broker.py`、
  `tests/test_trading_calendar.py`、`FRONTEND_STRATEGY_DEVELOPMENT_PLAN.md`、本文件。
