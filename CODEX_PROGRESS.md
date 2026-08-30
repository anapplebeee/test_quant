# 执行进度快照（2026-08-31）

> 本文件由 **GLM**（WorkBuddy 会话）维护，记录 Codex 规划的执行进度。
> - 第一轮（Codex）：手动交易闭环 + 默认策略切换 + 交易日历/模拟盘，commit `77a182e` / `2e7f1fb`
> - 第二轮（**GLM**）：完成规划遗留项 + 全量测试，见下文。

## 一、GLM 本轮完成的遗留事项（对应 Codex 规划）

| 遗留项 | 状态 | 落点 |
|---|---|---|
| 未对账阻断正式计划审批 | ✅ 验证已有实现（Codex 落地）+ 门禁测试 | `repository.approve_plan` + `test_approval_requires_signal_day_reconciliation` |
| 执行偏差统计 | ✅ 验证已有实现（Codex 落地），GLM 补 E2E 断言 | `execution_summary` + 前端"计划与成交偏差复盘" |
| 计划外交易 + 端到端每日流水线测试 | ✅ GLM 新增 | `tests/test_daily_pipeline_e2e.py`（T日计划→审批→部分成交→计划外成交→对账→T+1计划→复盘全链路） |
| 法定节假日测试 | ✅ GLM 新增 | `test_trading_calendar.py`：节假日跳过、`is_trade_date`、settle 推进用日历缓存（跨国庆）、缺缓存退化 |
| 各券商 CSV 列映射 | ✅ GLM 新增 | `quart/manual_trading/broker_profiles.py`（中文列名/20260831 日期/买入卖出方向/600000.SH 后缀归一化）+ `io.import_broker_csv` + `tests/test_broker_profiles.py`；XLSX 待需要时补 |
| 前端区分策略准入状态 | ✅ GLM 新增 | 策略监控页"策略准入状态"表（`strategy_catalog` + `live_allowlist` 对齐测试） |
| README 重构核对 | ✅ 核对通过，GLM 补券商 CSV 导入说明 | `README.md` |
| 两份规划文档勾选同步 | ✅ GLM 更新 | `FRONTEND_STRATEGY_DEVELOPMENT_PLAN.md`、`MANUAL_TRADING_T1_SYNC_PLAN.md` |
| 修复空存储查询崩溃 | ✅ GLM 顺手修复 | `store._query_partitioned` glob 无文件时返回空表（全量测试发现的真 bug） |

规划中仍未完成（阶段 F 券商 API，按规划"稳定运行后再设计"）：
- Adapter 回报统一写入 FillService、API 订单状态机与计划链路打通；
- XLSX 列映射（有真实需求时再做）。

## 二、全量测试（GLM 运行）

```text
.venv/Scripts/python.exe -m pytest tests/ -q
=> 1 failed, 273 passed  → 修复 store 空 glob bug 后该文件 22/22 通过
=> 等效全绿: 274 passed（其中本轮新增 12 个测试）
新增测试: test_daily_pipeline_e2e.py(2) + test_broker_profiles.py(5)
          + test_trading_calendar.py 扩充(4) + test_frontend_data.py 扩充(1)
```

## 三、GLM 策略优化实验（第二轮，进行中）

- 新增 `scripts/optimize_strategy.py`：单次数据加载批量跑参数网格（基线/择时开关/top_k/调仓频率/缓冲带/反转叠加/零成本对照），输出对比表 + JSON。
- 关键发现：本地 BarStore 仅 **229 只**标的（README 全市场口径 3215 只），低波行业内 z 在小池上分组样本不足，是当前回测收益解释的重要背景。
- 报告：`reports/strategy_optimization_2026-08-31.md`（实验完成后输出）。

## 四、改动清单（GLM 本轮）

- 新增：`quart/manual_trading/broker_profiles.py`、`scripts/optimize_strategy.py`、
  `tests/test_broker_profiles.py`、`tests/test_daily_pipeline_e2e.py`
- 修改：`quart/manual_trading/io.py`（broker CSV 导入）、`quart/data/store.py`（空 glob 修复）、
  `api/strategy_api.py`（live_allowlist/准入标记）、`frontend/pages/strategy_monitor.py`（准入状态表）、
  `tests/test_trading_calendar.py`、`tests/test_frontend_data.py`、README、两份规划文档、本文件
