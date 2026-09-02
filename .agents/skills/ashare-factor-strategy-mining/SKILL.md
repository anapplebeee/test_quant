---
name: ashare-factor-strategy-mining
description: Research, implement, audit, and validate A-share equity factors and daily T+1 strategies in Quart. Use when mining new factors, translating research into portfolio strategies, comparing candidates, diagnosing failed alpha, or preparing a strategy for Paper admission. Do not use for direct live brokerage execution or non-A-share assets.
---

# A 股因子与策略挖掘

Produce reproducible research evidence and an executable candidate without overstating profitability. Treat a useful factor, a viable portfolio, and a production-admitted strategy as three separate claims.

## Choose the work mode

- **Hypothesis discovery**: define economic rationale, data availability, direction, horizon, universe, controls, expected failure conditions, and comparison baseline before searching parameters.
- **Factor implementation**: add a point-in-time feature and an executable T+1 label; register it only after coverage and leakage checks pass.
- **Portfolio translation**: turn factor scores into weights through the Portfolio Constructor, including holdings, liquidity, turnover, cash, and exposure limits.
- **Validation/admission**: freeze parameters, run independent OOS/WFA and adverse execution scenarios, then evaluate the Admission Gate.
- **Failure diagnosis**: attribute failure to coverage, decay, redundancy, turnover, capacity, benchmark exposure, regime dependence, or overfitting. Preserve negative results.

For any mining, implementation, or validation task, read [references/research-protocol.md](references/research-protocol.md). When working in Quart, also read [references/quart-integration.md](references/quart-integration.md) before changing code or running project commands.

## Non-negotiable research invariants

1. Define the information timestamp. A value is usable only after `published_at` and supplier `available_at`; report-period dates and revised histories are not availability dates.
2. Default daily convention is signal after the T close and execution from T+1. A different convention must state what was observable and model a feasible session.
3. Use a point-in-time universe and security status. Missing historical membership, ST, suspension, listing/delisting, industry, or corporate-action evidence makes a formal conclusion fail closed.
4. Never select parameters on dates later called OOS. Record every tried family and apply FDR or another multiple-testing correction across the actual search family.
5. Compare against both an investable market benchmark and the eligible-universe equal-weight baseline. Positive absolute return alone is not alpha.
6. Separate a descriptive Top-basket result from a tradable strategy. The latter includes costs, turnover, ADV capacity, partial fills, execution scenarios, holdings, and portfolio risk.
7. Do not silently drop requested factors, fill unknowns with zero, or substitute future-filled data. Fail with missing fields, dates, symbols, and remediation.
8. Never edit `strategy.live_allowlist`, `strategy.paper_allowlist`, production parameters, or broker settings as a research side effect. Paper promotion requires an explicit user request, passing research/portfolio gates, and an auditable formal run. Live promotion is a separate decision and additionally requires sustained Paper execution, reconciliation evidence, and explicit authorization.

## Working procedure

1. Inspect git status, coordination/roadmap documents, factor and strategy registries, data contracts, and the latest relevant Artifact. Preserve unrelated changes.
2. Write a compact hypothesis card before implementation. Give the candidate a stable name and distinguish pre-specified choices from diagnostics.
3. Audit coverage and PIT semantics. If required data is absent, implement only the data contract or mark the research blocked; do not synthesize observations.
4. Implement the smallest reusable factor or strategy component. Keep calculations separate from loading, portfolio construction, execution, and presentation.
5. Add tests for timestamp boundaries, warm-up, missing data, direction, cross-sectional behavior, and no-future-data. Add portfolio/execution tests when weights or orders change.
6. Run factor diagnostics before portfolio tuning: coverage, RankIC/ICIR, split stability, recent decay, FDR, group returns, redundancy, turnover, and capacity proxy.
7. Build through standard portfolio and execution paths. Report benchmark-relative exposure, turnover, cost, capacity, and constraint usage.
8. Freeze the candidate and validate on untouched dates with continuous WFA plus cost, account-size, ADV, and open/VWAP/close scenarios. Follow-up tuning after OOS failure is diagnostic until new untouched data exists.
9. Save code/data versions, parameters, factor list, label convention, trials, metrics, and gate decision in Artifacts/reports. State `PASS`, `FAIL`, `PROVISIONAL`, or `BLOCKED` explicitly.

## Completion standard

A delivery identifies what changed, how it was tested, Artifact/report evidence, benchmark-relative results, and what remains unproven. Never promise stable profit. Recommend Paper admission only when configured research and portfolio gates pass; recommend live consideration only after sustained Paper execution and reconciliation evidence.
