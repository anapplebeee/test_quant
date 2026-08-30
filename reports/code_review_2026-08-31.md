# 全量代码与算法审查报告（2026-08-31，GLM）

> 审查范围：`quart/`（策略/风险/执行/数据/账本/broker）、`api/`、`frontend/pages/`、
> `scripts/`（33 个）、`common.py`、`config_schema.py`。
> 方法：2 个并行子代理广域扫描 + 主代理逐项验证（含实盘数据反推量价关系）。
> 结论：**11 项真实问题已修复，3 项口径变更类问题暂缓（需用户确认）。**
> 全量测试 **283 passed**（本轮 +4）。

## 一、已修复问题

### P0（数据错误/必然崩溃）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | `scripts/factor_research_ext.py:86,97` | 财务因子披露滞后用 `pd.Timedelta(days=120)` 自然日（≈84 交易日），财报提前约 36 交易日视为可用 → **ep/bp/roe 因子前视偏差、IC 虚高** | 改为在交易日序列中偏移 120 个交易日（searchsorted） |
| 2 | `scripts/diag_regime_band.py:31` / `diag_delisted_effect.py:71` | 直接 `Strategy(**cfg["strategy"])`：含 `live_allowlist/overrides` 等非参数键 → **必然抛"未知参数"崩溃**；且绕过 `resolve_params`（overrides 不生效） | 改走 `build_strategy`；`NoDelistedPickStrategy` 把 `delisted_symbols` 声明进 PARAMS_SCHEMA |

### P1（边界 bug / 前视 / 契约失效）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 3 | `quart/data/universe.py:38` | PIT 查询返回 `[]`（有历史但该日无成分，如指数未成立）被 `if pit:` 判假 → **静默回退当前快照，早期回测注入今日成分股（前视偏差）** | 区分 `None`（无历史→原回退逻辑）与 `[]`（→返回空股票池） |
| 4 | `quart/data/artifacts.py:90` | `data_version` 只取 `symbols[0]/[-1]` 两只票日期，其余股票更新不改变指纹 → **可复现性契约失效** | 全市场扫描最小/最大年份目录取全局 min/max |
| 5 | 4 个核心 CLI | `--save-dir default="reports"` 硬编码相对路径，依赖 CWD | 默认值改 `str(common.reports_dir())`（run_backtest/sweep/walk_forward/baseline_random） |
| 6 | `scripts/optimize_strategy.py` | 缺 `bars.empty` 空检查；`--save` 指向 `.md` 实际只写 `.json` | 补空检查；参数名/说明改为 json |
| 7 | `scripts/factor_research.py:103` | 采样全跳过时 `pd.Series([])` 产生 NaN 污染结果表 | `if not ics: continue` |
| 8 | `scripts/train_ml.py:102` | `months` 为空时 `months[0]` IndexError | 空时 SystemExit 给出明确提示 |

### P2（数据/容错/配置）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 9 | `quart/data/source_akshare.py:149-159` | 腾讯源抛异常直接上抛，**东财兜底永不执行**，单票永久拉取失败 | `try` 包裹腾讯，异常统一走兜底 |
| 10 | `quart/data/source_akshare.py` **腾讯源 volume 单位不一致** | 实测（量价关系反推）：`stock_zh_a_hist_tx` 对 **000 开头返回手、其余（002/300/6xx/688）返回股**，横截面差 100 倍 → **volume 类因子（net_flow20/volume_ratio20）跨股票失真** | 新增 `_normalize_volume_unit`：按 `amount/(close×volume)` 中位比值自动归一化为手（与东财一致），+4 单元测试 |
| 11 | `quart/config_schema.py` | SPEC 缺 `lookback_days/regime_*/min_avg_amount/liquidity_days/min_price/overrides/notify.*` 等实际使用键 | 补 11 个键（可选键类型校验） |

## 二、暂缓问题（口径变更，需用户确认后实施）

| 位置 | 问题 | 为什么暂缓 |
|---|---|---|
| `quart/data/universe.py:102` filter_st | 用**当前** ST 名单过滤全部历史（ST 期照常可买/现 ST 的历史被剔），双向前视 | 修复会改变回测口径与全部历史结论，需先明确"历史 ST 时段剔除"的正式口径 |
| `quart/data/market.py` vs `benchmark.py` | 策略用 `close_val`(ffill) 而等权基准用 raw `closes`，停牌复牌跳空处理不一致 → alpha 归因偏差 | 需统一基准构建口径并重跑全量基准，属策略结论变更 |
| `quart/data/updater.py` + `repair_qfq_negative.py` | 全量刷新从 20190101 起 `replace=True` 只覆盖 2019+ 分区，钉住股票 2019 前残留 qfq、2019 后为 hfq，价格水平在边界跳变 | 需按钉住日整史重拉 + 数据迁移，涉及存量数据，单独一轮处理 |

## 三、审查方法说明（防误报）

- 子代理发现的问题全部经主代理**代码级验证**后才修复；无法复现/属风格偏好的未采纳
- volume 单位问题以**实盘数据量价关系反推**（`amount/(close×volume)`：000001≈99.5、600519≈1.0、300750≈1.0、688981≈1.0），并用腾讯源直接拉取复核后确认
- 参数优先级（显式 > overrides > 全局）在 run_backtest/sweep/walk_forward 中复核无问题；引擎 T+1 语义、滑点方向（BUY+slip/SELL-slip）复核无问题

## 四、复现命令

```powershell
# 修复后全量回归
.venv/Scripts/python.exe -m pytest tests/ -q
# 财务因子修复后重跑（IC 会变保守，属预期）
.venv/Scripts/python.exe scripts/factor_research_ext.py
```
