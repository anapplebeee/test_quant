"""正式策略类 small_value 复现验证：应与研究方向 SmallCapStrategy(H5) 的 30.88% 一致。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest_small_val import BarStore, load_config, load_md, run_once  # noqa: E402
from quart.strategy import build_strategy  # noqa: E402


def main():
    md = load_md()
    bt = load_config()["backtest"]
    strat = build_strategy("small_value")  # settings.overrides.small_value = H5 参数
    r = run_once(md, bt, strat, "small_value-formal")
    print(f"正式类: CAGR {r['cagr']*100:7.2f}%  MDD {r['mdd']*100:7.2f}%  "
          f"Sharpe {r['sharpe']:5.2f}  换手 {r['annual_turnover']*100:5.0f}%  "
          f"终值 {r['final']:>10,.0f}  (基准 H5=30.88%/-35.05%/1.18/233,078)")


if __name__ == "__main__":
    main()
