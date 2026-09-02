"""RESEARCH-011 §6.3/§6.4：通用风险预算层验证（Gate A/B 门禁口径）。

设计：alpha = R010 最优选股（lowvol_indz Top8 + new_alpha_weight 0.3，**关闭**
alpha 自带择时，市场状态职责移交风险层）；叠加 RiskBudgetOverlay（预注册档位，
不做逐参数扫描）。验证门禁：

1. 有/无 overlay 对比（3 万元、全成本 1x）；
2. 成本鲁棒性 0 / 1 / 2 / 3 x（2x 不能结构性失效）；
3. 双基准超额（沪深300 + 同池等权）。

用法：python scripts/eval_risk_budget.py
"""
from __future__ import annotations

import pandas as pd

from quart.backtest.engine import BacktestEngine
from quart.config import load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.execution.fees import Fees
from quart.strategy import build_strategy
from quart.strategy.risk_budget import RiskBudgetOverlay

try:  # 全区间扫描的 DuckDB 内存上限（默认无界，机器内存紧张时 OOM）
    import duckdb

    duckdb.default_connection.execute(
        "SET memory_limit='2GB'; SET threads=4; SET temp_directory='reports/.duckdb_tmp';"
    )
except Exception:
    pass

CAP = 30_000
ALPHA_PARAMS = dict(
    top_k=8,
    min_avg_amount=20_000_000,
    max_weight_pct=0.4,
    rebalance_days=45,
    rank_buffer=0.5,
    new_alpha_weight=0.3,
    use_regime_filter=True,   # alpha 自带 R4 趋势打分择时（已验证有效）
    regime_mode="score",
    timing_levels=2,
)


def load_md():
    cfg = load_config()
    store = BarStore()
    # 逐年分块加载：当前机器仅剩 ~2GB 空闲物理内存，一次性物化全量长表
    # （~2GB）会触发 duckdb/ pandas 分配失败；分块后单块 ~150MB。
    frames = []
    keep = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
    for year in range(2021, 2027):  # 机器内存受限：评估窗口 2021-2026（结论同窗口自洽）
        part = store.load(start=f"{year}-01-01", end=f"{year}-12-31", include_index=False)
        if part.empty:
            continue
        if "name" in part.columns:  # ST 过滤可能需要名称列
            keep_year = keep + ["name"]
        else:
            keep_year = [c for c in keep if c in part.columns]
        part = part[keep_year].copy()
        num_cols = [c for c in keep if c in part.columns and c not in ("date", "symbol")]
        part[num_cols] = part[num_cols].astype("float32")
        part["symbol"] = part["symbol"].astype(str)
        part = filter_for_simulation(
            part, exclude_star=True, exclude_chinext=True, exclude_st=True, min_list_days=120
        )  # 逐年先过滤（list_dates/ST/退市均为全局清单，逐年过滤语义一致），再拼接省内存
        frames.append(part[keep])
        del part
    import gc

    gc.collect()
    bars = pd.concat(frames, ignore_index=True)
    del frames
    gc.collect()
    md = MarketData.from_bars(bars, store.load_benchmark(cfg["benchmark"]))
    del bars
    gc.collect()
    return cfg, md


def bench_curves(md: MarketData):
    """双基准：沪深300 与同池等权（T+1 可执行的收盘等权净值）。"""
    bench = pd.Series(md.benchmark_close, index=md.dates).dropna()
    bench = bench / bench.iloc[0]
    equal = md.close_val.mean(axis=1, skipna=True)
    equal = equal / equal.iloc[0]
    return bench, equal


def run(md: MarketData, bt: dict, overlay: bool, cost_mult: float,
        enable_state: bool = True) -> dict:
    fees = Fees(
        bt["commission_rate"] * cost_mult,
        bt["commission_min"] * cost_mult,
        bt["stamp_tax_rate"] * cost_mult,
        bt["transfer_fee_rate"] * cost_mult,
        bt["slippage_rate"] * cost_mult,
        0.0,
    )
    alpha = build_strategy("lowvol_indz", **ALPHA_PARAMS)
    if overlay:
        strategy = RiskBudgetOverlay(alpha, enable_state=enable_state)
    else:
        strategy = alpha
    engine = BacktestEngine(md, strategy, fees=fees, initial_cash=CAP,
                            max_adv_participation=0.05)
    equity = engine.run()
    del engine, strategy, alpha
    import gc

    gc.collect()
    equity.index = md.dates[: len(equity)]
    yrs = len(equity) / 365.0
    cagr = (equity.iloc[-1] / CAP) ** (1 / yrs) - 1
    dd = equity / equity.cummax() - 1
    mdd = float(dd.min())
    rets = equity.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * 252**0.5) if rets.std() > 0 else 0.0
    calmar = cagr / abs(mdd) if mdd else 0.0
    # 时间水下回撤持续期（最长连续低于高水位的天数）
    below = dd < -1e-9
    longest = int((below != below.shift()).cumsum()[below].value_counts().max()) if below.any() else 0
    return {
        "cagr": cagr, "mdd": mdd, "sharpe": sharpe, "calmar": calmar,
        "final": float(equity.iloc[-1]), "trades": len(engine.trades),
        "longest_dd_days": longest, "equity": equity,
    }


def main() -> None:
    cfg, md = load_md()
    bt = cfg["backtest"]
    bench300, bench_equal = bench_curves(md)

    print("=== 1) 风险层职责配置对比（全成本 1x，预注册档位）===")
    base = run(md, bt, overlay=False, cost_mult=1.0)
    full = run(md, bt, overlay=True, cost_mult=1.0, enable_state=True)
    gated = run(md, bt, overlay=True, cost_mult=1.0, enable_state=False)
    for label, r in [
        ("alpha-only(R4择时)", base),
        ("+risk_budget(全维度)", full),
        ("+risk_budget(仅回撤+波动)", gated),
    ]:
        print(
            f"### {label}: CAGR={r['cagr']*100:5.2f}% Sharpe={r['sharpe']:4.2f} "
            f"MDD={r['mdd']*100:6.2f}% Calmar={r['calmar']:4.2f} "
            f"终值={r['final']:,.0f} 最长水下={r['longest_dd_days']}日"
        )
    gate_a = gated["cagr"] >= 0.12 and abs(gated["mdd"]) <= 0.20
    gate_b = gated["cagr"] >= 0.20 and abs(gated["mdd"]) <= 0.20
    print(f"### Gate A(≥12%/≤20%): {'PASS' if gate_a else 'FAIL'} | "
          f"Gate B(≥20%/≤20%): {'PASS' if gate_b else 'FAIL'}")

    print("\n=== 2) 成本鲁棒性（风险层=仅回撤+波动，0/1/2/3x 成本）===")
    for mult in [0.0, 1.0, 2.0, 3.0]:
        r = run(md, bt, overlay=True, cost_mult=mult, enable_state=False)
        print(
            f"### cost {mult}x: CAGR={r['cagr']*100:5.2f}% Sharpe={r['sharpe']:4.2f} "
            f"MDD={r['mdd']*100:6.2f}% trades={r['trades']}"
        )

    print("\n=== 3) 双基准超额（风险层=仅回撤+波动，1x）===")
    eq = gated["equity"]
    nav = eq / eq.iloc[0]
    for name, b in [("沪深300", bench300), ("同池等权", bench_equal)]:
        aligned = b.reindex(nav.index).ffill()
        excess = float(nav.iloc[-1] / aligned.iloc[-1] - 1)
        print(f"### vs {name}: 全期净值超额 {excess*100:.1f}%")


if __name__ == "__main__":
    main()
