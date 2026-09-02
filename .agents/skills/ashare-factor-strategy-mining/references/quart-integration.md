# Quart 集成指引

Use this reference only in the Quart A-share platform workspace.

## Read before acting

- `docs/DEVELOPMENT_COORDINATION.md`: ownership, completion, and no-fabrication rules.
- `QUANT_PLATFORM_REFACTOR_ROADMAP.md`: target architecture and milestone exit gates.
- `docs/量化平台能力缺口与交付路线_2026-09-01.md`: latest audited status and gaps.
- `docs/FACTOR_AUDIT_PROTOCOL.md`: label and provisional-baseline contract.
- Latest relevant research report and Artifact manifest; never infer status from filenames alone.

## Canonical paths

| Concern | Path |
|---|---|
| Factor specifications/calculation | `quart/research/factor_audit.py` |
| PIT event transforms | `quart/research/event_factors.py` |
| PIT financial inputs | `quart/data/financials_pit.py`, `quart/data/fundamental.py` |
| Event contracts/loaders | `quart/data/announcements.py`, `scripts/fetch_announcements.py`, `scripts/fetch_dragon_tiger.py`, `data/events/news.parquet`, `data/events/dragon_tiger.parquet` |
| Factor audit CLI | `scripts/factor_audit.py` |
| Strategy registry | `quart/strategy/__init__.py` |
| Strategy UI/API metadata | `api/strategy_api.py` |
| Dynamic parameters | strategy `PARAMS_SCHEMA`, `quart/strategy/parameters.py` |
| Factor-to-portfolio strategy | `quart/strategy/factor_portfolio.py` |
| Benchmark-constrained template | `quart/strategy/index_enhancement.py` |
| Portfolio construction | `quart/portfolio/constructor.py`, `quart/portfolio/context.py` |
| PIT exposures | `quart/data/exposure_store.py`, `quart/risk/exposure.py` |
| Backtest/execution | `quart/backtest/engine.py`, `quart/execution/` |
| Formal audit/gates | `quart/research/formal_audit.py`, `quart/research/admission.py` |
| Artifacts | `quart/data/artifacts.py` |

Do not create a parallel registry, order simulator, or report-only source of truth.

## Typical commands

```powershell
uv run python scripts/data_snapshot.py build
uv run python scripts/factor_audit.py --sample weekly --horizon 5 --evaluation-start 2023-01-01 --factor FACTOR_NAME
uv run python scripts/run_backtest.py --strategy factor_portfolio --param factor_names=FACTOR_A,FACTOR_B --param rebalance_days=20
uv run python scripts/research_audit.py --strategy STRATEGY --start 2020-01-01 --oos-start YYYY-MM-DD
uv run python scripts/admission_gate.py --strategy STRATEGY --no-apply
```

Use targeted tests first, then appropriate broader checks. Full-data runs may be expensive; run them when required and state when they were not run.

## Quart-specific rules

- Factor-audit Top baskets are provisional and cannot justify allowlist changes.
- Missing requested PIT factors fail; do not silently remove them from `factor_names`.
- `factor_portfolio` and `index_enhancement` produce weights through `PortfolioConstructor` and retain its receipt.
- Exposure constraints require a valid `ExposureSnapshot`; current classifications cannot stand in for missing PIT history.
- Formal runs pass `QualityGate`; exploratory outputs retain `NON_PIT`/`DEGRADED` labels.
- Keep `config/settings.yaml` defaults, `strategy.paper_allowlist`, and `strategy.live_allowlist` unchanged during mining. Admission-ledger writes and promotion are separately authorized operations.

## Known integration traps that require explicit checks

These are current repository limitations, not optional preferences:

1. `scripts/research_audit.py` and `scripts/admission_gate.py` do not by themselves prove complete PIT formality: the internal single backtest checks data quality but does not currently force the same PIT-universe evidence as `run_backtest --research-mode formal`, and its WFA subprocess does not force formal mode. Before a formal/Paper conclusion, separately require successful formal runs such as:

   ```powershell
   uv run python scripts/run_backtest.py --strategy STRATEGY --research-mode formal
   uv run python scripts/walk_forward.py --strategy STRATEGY --research-mode formal
   ```

   If official corporate-action, security-state, universe, or exposure files are absent, keep the result `PROVISIONAL`/`BLOCKED` even if the Admission Gate calculation prints PASS.

2. Current WFA grid parsing splits comma-separated values. Passing `--param factor_names=a,b,c` through `research_audit.py` can be interpreted as three single-factor WFA candidates rather than one fixed multi-factor combination. Do not use that route as OOS proof for a multi-factor basket. First add a fixed-parameter/escaped-list contract with tests, or register a strategy whose combination is unambiguous.

3. The default Artifact snapshot provenance primarily pins daily and index datasets. Financial, fundamental, event, universe, and exposure files may not automatically alter the run fingerprint. A formal report must record every supplemental input path plus content hash/version and availability coverage; otherwise downgrade the conclusion. For event research, explicitly pin `data/events/news.parquet`, `data/events/dragon_tiger.parquet`, and the announcement sentiment rule or trained-model version used to create the score.

4. `scripts/mine_factors.py` can warn and continue when optional fundamentals/news/龙虎榜 inputs are missing, and it writes exploratory CSV rather than a complete Artifact. Use it only for discovery. `run_factor_audit` may also omit a registered factor whose panel is unavailable, so verify every requested factor appears in the returned summary/metadata with acceptable coverage. Process exit success is not evidence that all requested factors ran.

   Event candidates do not currently flow automatically from `scripts/mine_factors.py` into the formal factor registry/audit. Before treating one as formally audited, add its deterministic calculation and specification to the canonical audit path, preserve the event-active mask, add focused PIT tests, and verify the requested factor is present in the Artifact metadata.

5. `event_sentiment_panels` currently represents inactive stock-dates with numeric zero while decaying state across the full panel. This is acceptable only for exploratory attention/signal construction, not as proof that a selectively disclosed event factor covers the market. Formal event research must retain a separate active mask, use `NaN` for unobserved stock-dates in cross-sectional statistics, and report an event-active equal-weight baseline. Add tests that distinguish no event, a disclosed zero, and a decayed prior event.

6. `factor_portfolio` fails closed when a requested factor panel is unavailable; prefer this behavior for portfolio validation and keep it in new strategy paths.

7. A new named strategy is not integrated by adding one class alone. It needs a `PARAMS_SCHEMA`, `REGISTRY` entry, `api/strategy_api.py` metadata, factor-execution receipt, Portfolio Constructor/risk context, and focused tests. Do not bypass the Constructor with unaudited final weights.

## Expected delivery

Return the hypothesis, changed files, tests/commands, Artifact run ID, diagnostics, gate label with failed checks, and next evidence required. Update status documents only when code and evidence support the new state.
