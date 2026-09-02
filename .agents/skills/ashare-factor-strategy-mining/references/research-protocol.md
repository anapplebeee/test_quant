# A 股研究协议

Use this reference whenever the task mines, evaluates, combines, or promotes an A-share factor or strategy.

## Hypothesis card

Record before computing results:

- stable candidate ID and factor family;
- economic/behavioral mechanism and expected direction;
- inputs, transformations, neutralization, winsorization, and missing-value rule;
- decision and execution timestamps, holding horizon, rebalance frequency, and warm-up;
- universe, benchmark, industry/size/liquidity controls, and capacity assumption;
- pre-specified parameter set, failure criteria, and expected redundant factors.

After viewing validation results, label new variants as diagnostics. Do not relabel their shared interval as independent OOS.

## Point-in-time and A-share execution checks

- Membership, listing/delisting, ST/risk-warning, suspension, board, industry, shares, and corporate actions resolve by decision date.
- Financial/event data require publication time and system/vendor arrival time. Revisions create a new version; they do not rewrite the past snapshot.
- For a T-close signal, do not execute at the T close. Default entry is T+1 open; VWAP/close are declared stress scenarios.
- Apply historical price limits, new-listing, lot-size, T+1 sellability, suspension, and delisting through shared RuleBook/SecurityMaster paths.
- Adjusted prices do not replace a corporate-action ledger. Keep raw price, adjustment factor, cash distribution, and share change auditable.
- For selectively disclosed datasets such as 龙虎榜, emit an explicit event-active mask. Keep inactive/unobserved stock-dates as `NaN`, not numeric zero; distinguish a disclosed zero from no disclosure.

If these cannot be evidenced, mark the result `PROVISIONAL` or `BLOCKED`, not formally validated.

## Factor diagnostics

At minimum report:

- overall, early/late, and recent coverage;
- cross-sectional RankIC mean, ICIR, positive-IC ratio, and history;
- FDR-adjusted significance across all factors/variants actually tried;
- quantile/Top-basket returns versus same-day eligible-universe equal weight; for selectively disclosed events, also compare with the same-day event-active equal-weight baseline;
- monotonicity and long-only Top-minus-universe return, not only long-short spread;
- decay across relevant horizons and consistency with intended holding period;
- rank correlation/redundancy with existing factors;
- turnover, ADV distribution, price-limit/suspension rejection rate, and capacity proxy;
- industry, size, Beta, volatility, and liquidity attribution;
- bull/bear/sideways, high/low volatility, liquidity, industry, and size subperiod behavior.

Prefer economic magnitude and implementability over a marginal p-value. High IC with poor coverage, extreme turnover, concentrated eras, or inaccessible entry prices is not viable.

## Strategy validation hierarchy

Evaluate in order:

1. deterministic factor and timestamp tests;
2. eligible-universe equal-weight and market-index baselines;
3. fixed-rule factor portfolio without OOS tuning;
4. rolling/expanding continuous WFA with warm-up and unique OOS dates;
5. default/adverse costs, ADV participation, account sizes, partial fills, and price scenarios;
6. parameter-neighborhood stability and contribution concentration by year/industry/symbol;
7. bootstrap/confidence and multiple-testing-aware diagnostics;
8. Paper execution, fill/slippage calibration, account reconciliation, and incidents.

Report absolute/relative return, Sharpe/Sortino, drawdown and duration, tracking error/information ratio, turnover, costs, trade count, capacity, and constraint violations. Do not optimize the conclusion around one CAGR.

## Decision labels

- `PASS`: configured data, research, portfolio, cost/capacity, and OOS gates passed for the stated scope.
- `FAIL`: a valid test completed and pre-specified gates failed. Preserve results and failed checks.
- `PROVISIONAL`: evidence exists but PIT coverage, untouched sample, execution, or formal evidence is incomplete.
- `BLOCKED`: required data, authority, or external system is absent; a meaningful test would require fabrication.

Factor `PASS` does not imply strategy `PASS`; research/portfolio `PASS` does not imply live permission.

## Research priorities

Prefer orthogonal data and strategy breadth over correlated price-volume variants:

- PIT quality/value: profitability quality, accruals, cash-flow quality, asset growth, dividend yield, EP/BP/GARP;
- capital/event: financing, northbound or eligible institutional flows, 龙虎榜, announcements, earnings surprises, limit-up failure/crowding;
- robust price-volume: multi-horizon momentum with short reversal removed, residual momentum, low risk, and liquidity with crowding controls;
- portfolio: industry/size/style neutrality, benchmark-relative risk, turnover buffers, ADV constraints, and state-aware risk budgets.

Sequence models only after the linear/fixed-rule baseline is trustworthy. Complexity must prove incremental untouched-sample value after costs.
