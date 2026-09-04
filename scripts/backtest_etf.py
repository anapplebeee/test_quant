"""ETF 动量轮动策略(etf_momentum)的官方回测入口。

喂"9 只 ETF 小 panel"给平台 BacktestEngine。ETF 无 A 股个股涨跌停语义，因此使用
`BacktestExecutionModel(enforce_limits=False)`（绕开 RuleBook 涨跌停/拒单；ETF 撮合
仍按整手+成本，见下）。不含入 A 股全市场流程(run_backtest.py)。

用法
----
    # 先入库真实 ETF 日线(需东财可用)：
    .venv/Scripts/python.exe scripts/update_etf.py
    # 再回测(读本地入库的 ETF 数据)：
    .venv/Scripts/python.exe scripts/backtest_etf.py
    .venv/Scripts/python.exe scripts/backtest_etf.py --start 20200101 --etfs "510300,510500,511010"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.backtest.engine import BacktestEngine  # noqa: E402
from quart.data.market import MarketData  # noqa: E402
from quart.data.store import BarStore  # noqa: E402
from quart.execution.backtest_model import BacktestExecutionModel  # noqa: E402
from quart.strategy import build_strategy  # noqa: E402

DEFAULT_ETFS = ["510300", "510500", "159915", "588000", "512890",
                "518880", "159920", "513500", "511010"]


def load_etf_bars(etfs: list[str], start: str, end: str) -> pd.DataFrame:
    store = BarStore()
    present = [c for c in etfs if c in set(store.symbols())]
    missing = [c for c in etfs if c not in present]
    if missing:
        raise SystemExit(
            f"本地缺少 ETF 数据: {missing}。请先运行 .venv/Scripts/python.exe "
            f"scripts/update_etf.py 入库真实 ETF 日线"
        )
    df = store.load(symbols=present, start=start, end=end)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--etfs", default=",".join(DEFAULT_ETFS))
    ap.add_argument("--start", default="20150101")
    ap.add_argument("--end", default="20260831")
    ap.add_argument("--defense_etf", default="511010")
    ap.add_argument("--top_n", type=int, default=2)
    args = ap.parse_args()

    etfs = [c.strip() for c in args.etfs.split(",") if c.strip()]
    bars = load_etf_bars(etfs, args.start, args.end)
    md = MarketData.from_bars(bars, benchmark=None)
    print(f"panel: {len(etfs)} ETF, {md.dates[0].date()} ~ {md.dates[-1].date()} "
          f"({len(md.dates)} 交易日)")
    print("symbols:", list(md.close_val.columns))

    strat = build_strategy("etf_momentum", top_n=args.top_n,
                           defense_etf=args.defense_etf)
    # ETF 专用撮合口径：
    #   - enforce_limits=False：ETF 无 A 股个股涨跌停(绕开 RuleBook 拒单)
    #   - ETF 专用 Fees：场内 ETF 卖出无印花税/无过户费；滑点按 ETF 窄价差取 0.0003(对齐原 etf.py)
    #     而非个股 slippage_rate=0.001（平台默认 A 股 Fees 会误收卖出印花税并放大周频换手成本）
    from quart.execution.fees import Fees as _Fees

    etf_fees = _Fees(
        commission_rate=0.00025, commission_min=5.0,
        stamp_tax_rate=0.0, transfer_fee_rate=0.0,
        slippage_rate=0.0003, impact_coef=0.0,
    )
    exec_model = BacktestExecutionModel(fees=etf_fees, enforce_limits=False)
    eng = BacktestEngine(md, strat, initial_cash=1_000_000, execution_model=exec_model)
    res = eng.run_result()
    eq = res.equity
    ret = eq.pct_change().dropna()
    years = len(eq) / 244
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    mdd = (eq / eq.cummax() - 1).min()
    sharpe = ret.mean() / ret.std() * np.sqrt(244) if ret.std() > 0 else float("nan")

    # 对照：9 只等权买入持有 & 沪深300ETF(510300)买入持有
    closes = md.close_val
    eqw = (closes.pct_change().fillna(0)).mean(axis=1)
    nav_eq = (1 + eqw).cumprod().reindex(eq.index)
    nav_eq = nav_eq / nav_eq.iloc[0]
    eq_ann = nav_eq.iloc[-1] ** (1 / years) - 1
    eq_mdd = (nav_eq / nav_eq.cummax() - 1).min()

    bench = None
    if "510300" in closes.columns:
        bh = closes["510300"].reindex(eq.index)
        nav_bh = bh / bh.iloc[0]
        bench = nav_bh.iloc[-1] ** (1 / years) - 1

    print(f"\n策略(etf_momentum Top{args.top_n}):")
    print(f"  期末净值 {eq.iloc[-1]:.4f} | 年化 {ann:.2%} | 最大回撤 {mdd:.2%} | 夏普 {sharpe:.2f}")
    print(f"  等权持有: 年化 {eq_ann:.2%} | 回撤 {eq_mdd:.2%}  [vs策略 {(ann-eq_ann)*100:+.1f}pp]")
    if bench is not None:
        print(f"  510300持有: 年化 {bench:.2%}  [vs策略 {(ann-bench)*100:+.1f}pp]")

    Path("reports").mkdir(exist_ok=True)
    eq.to_csv("reports/etf_momentum_equity.csv")
    print("\nequity 已存 reports/etf_momentum_equity.csv")


if __name__ == "__main__":
    main()
