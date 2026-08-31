# 因子审计协议与临时基线

## 状态与边界

本协议实现了 `DEVELOPMENT_COORDINATION.md` 中批次 0 的 C 泳道交付：在现有本地数据上输出可复现的因子审计与临时基线。所有产物均标记为 `provisional`，不能修改 `strategy.live_allowlist`、正式策略参数或准入状态。

升级为正式研究之前，必须完成 `DATA-001` 的内容哈希快照，并提供逐交易日 PIT 股票池、证券状态、行业与历史 RuleBook。

## 审计口径

- 信号：T 日收盘后可见的因子值；每个因子均定向为“数值越高越好”。
- 标签：T+1 开盘买入，持有 `horizon` 个交易日后于开盘卖出；默认是 T+1 至 T+6 开盘。
- 可交易性：入场和出场成交量必须大于零，且开盘不能处在对应证券板位的涨跌停价；候选还必须通过 20 日平均成交额下限。
- 统计：按月或按周横截面计算 Rank IC、前/后半段与最近窗口稳定性、FDR q-value、Top 10% 多空/多头收益、Top 篮子换手及因子排名相关性。
- 缺失数据：低于最小截面数的日期不进入统计；未知因子直接失败，不静默跳过。

## 临时基线

每个因子额外生成一个非重叠 Top 10% 标签篮子基线，与同日合格股票等权标签对照，报告：

- 标签篮子的年化收益、最大回撤和相对等权标签收益；
- 年化 Top 篮子换手；
- Top 篮子 20 日成交额中位数；
- `capacity_proxy_m`：成交额中位数的 10%，仅是流动性代理，不是可投资容量。

该基线是无费用、无冲击成本、无持仓约束的描述性统计，不得被表述为可实盘交易的回测结果。正式准入仍须单独完成成本压力、容量模型、连续 OOS/WFA 和 Admission Gate。

## 运行与产物

```powershell
uv run python scripts/factor_audit.py --sample monthly --horizon 5
```

运行会写入同一 Artifact run：`summary`、`ic_history`、`correlation`、`provisional_baseline` 与 `metadata`；也会生成以下兼容报表：

- `reports/factor_audit_summary.csv`
- `reports/factor_audit_ic_history.csv`
- `reports/factor_audit_correlation.csv`
- `reports/factor_audit_provisional_baseline.csv`
- `reports/factor_audit_metadata.json`

元数据记录数据区间、样本、标签、筛选条件与 `provisional_reason`。研究结论必须同时引用该元数据和对应 Artifact run ID。
