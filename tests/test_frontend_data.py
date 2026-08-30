"""前端数据层回归：策略清单单一数据源 + 研究产物 API。

背景（2026-08-28 审计）：首页/策略监控曾硬编码 3 个策略与 REGISTRY 漂移，
新验证结果（sweep csv / 研究报告 md）在前端无任何入口。
"""

from quart.strategy import REGISTRY
from api.strategy_api import (
    STRATEGY_META,
    get_strategy_defaults,
    live_allowlist,
    live_signal_choices,
    strategy_catalog,
    strategy_choices,
)
from api.research_api import (
    latest_sweep_headlines,
    list_research_reports,
    list_sweeps,
    load_research_report,
    load_sweep,
    sweep_headline,
)


def test_strategy_choices_covers_registry():
    choices = strategy_choices()
    assert choices == sorted(REGISTRY.keys())
    # 新验证的低频变体必须在前端可见
    assert "lowvol_indz" in choices
    assert "dual_ma" in choices


def test_live_signal_choices_excludes_research_strategies():
    assert live_signal_choices() == ["lowvol_indz"]


def test_meta_covers_registry():
    assert set(STRATEGY_META.keys()) == set(REGISTRY.keys())


def test_catalog_defaults_consistent():
    for row in strategy_catalog():
        assert row["name"] in REGISTRY
        assert isinstance(row["default_rebalance"], int) and row["default_rebalance"] > 0
        assert isinstance(row["default_top_k"], int) and row["default_top_k"] > 0


def test_catalog_marks_admitted_strategies():
    """准入状态必须与 live_allowlist 严格对齐，防止前端口径漂移。"""
    allowlist = set(live_allowlist())
    for row in strategy_catalog():
        expected = "✅ 准入" if (not allowlist or row["name"] in allowlist) else "🔬 研究"
        assert row["admitted"] == expected
    admitted_names = {
        row["name"] for row in strategy_catalog() if row["admitted"] == "✅ 准入"
    }
    assert admitted_names == set(live_signal_choices())


def test_defaults_lowvol_indz_uses_override():
    # settings.yaml: 正式候选采用 45 日 / Top30
    d = get_strategy_defaults("lowvol_indz")
    assert d["rebalance_days"] == 45
    assert d["top_k"] == 30
    # 无 override 的策略回退全局默认
    d2 = get_strategy_defaults("ml_rank")
    assert d2["rebalance_days"] >= 1


def test_research_api_sweeps():
    files = list_sweeps()
    assert all(not f.startswith("sweep_equity_") for f in files)
    assert files == sorted(files)
    if not files:
        return
    df = load_sweep(files[-1])
    assert df is not None and not df.empty
    head = sweep_headline(df)
    assert head is not None and len(head) <= 8
    assert "label" in head.columns
    # 非法名/越界名安全返回 None
    assert load_sweep("../settings.yaml") is None
    assert load_sweep("sweep_equity_x.csv") is None


def test_latest_sweep_headlines_shape():
    heads = latest_sweep_headlines()
    if heads.empty:
        return
    assert "策略" in heads.columns and "CAGR" in heads.columns
    # 每个策略只保留最新一条
    assert heads["策略"].is_unique


def test_research_reports_reader():
    files = list_research_reports()
    assert all(f.endswith(".md") for f in files)
    assert load_research_report("../app.py").startswith("⚠️")
    assert load_research_report("不存在的报告.md").startswith("⚠️")
    if files:
        content = load_research_report(files[-1])
        assert isinstance(content, str) and len(content) > 0
