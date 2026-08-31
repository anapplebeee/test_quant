"""前端页面构建冒烟测试。

CI 环境装不了 gradio/plotly（重且有编译依赖），但页面构建期的
NameError / 未定义变量 / 参数写错会导致**整个 Tab 白屏**，
必须在 CI 挡住。这里用 MagicMock 顶替 UI 库，只验证 Python 层能跑通。

能捕获：NameError、AttributeError、TypeError、解包长度不匹配、
调用不存在的组件参数（在真实 gradio 下才报错的部分除外）。
不能捕获：布局/样式问题——那需要真实渲染，不在 CI 范围。
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def ui_stubs(monkeypatch):
    """把 gradio / plotly 替换成 MagicMock，并清除前端模块缓存。"""
    gradio = MagicMock()
    plotly_go = MagicMock()

    # context manager（with gr.Tab() / gr.Accordion() / gr.Row()）
    for mod in (gradio,):
        for name in ("Tab", "Accordion", "Row", "Column", "Group", "Blocks"):
            cm = getattr(mod, name)
            cm.return_value.__enter__ = MagicMock(return_value=None)
            cm.return_value.__exit__ = MagicMock(return_value=False)

    monkeypatch.setitem(sys.modules, "gradio", gradio)
    # plotly 是包，需要占位成"是包"，否则 `import plotly.express` 报
    # "No module named 'plotly.express'; 'plotly' is not a package"
    plotly_pkg = MagicMock()
    plotly_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "plotly", plotly_pkg)
    monkeypatch.setitem(sys.modules, "plotly.graph_objects", plotly_go)
    monkeypatch.setitem(sys.modules, "plotly.express", MagicMock())

    for name in [m for m in sys.modules if m.startswith(("frontend",))]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    return gradio


def _import_page(name: str):
    return importlib.import_module(f"frontend.pages.{name}")


# ---------------------------------------------------------------- 单页构建


def test_backtest_page_builds(ui_stubs):
    mod = _import_page("backtest")
    mod.render()
    assert ui_stubs.Tab.called, "应创建 Tab 容器"


def test_backtest_page_registers_wfa_controls(ui_stubs):
    """WFA 面板的控件必须真的挂上——否则用户点了没反应。"""
    _import_page("backtest").render()

    dropdown_labels = [str(c.kwargs.get("label", "")) for c in ui_stubs.Dropdown.call_args_list]
    assert any("参数选择指标" in x for x in dropdown_labels), "缺少指标选择下拉"

    # 训练/测试/隔离天数用 Number 输入
    number_labels = [str(c.kwargs.get("label", "")) for c in ui_stubs.Number.call_args_list]
    assert any("训练窗口" in x for x in number_labels), "缺少训练窗口输入"
    assert any("测试窗口" in x for x in number_labels), "缺少测试窗口输入"
    assert any("隔离" in x for x in number_labels), "缺少隔离天数输入"


def test_backtest_page_wires_refresh_callbacks(ui_stubs):
    """刷新按钮必须绑定回调（.click 被调用）。"""
    _import_page("backtest").render()
    assert ui_stubs.Button.called
    assert ui_stubs.Button.return_value.click.called


# ---------------------------------------------------------------- 全部页面


def test_all_pages_build(ui_stubs):
    """任一页面崩溃都会让整个 Gradio 应用起不来，逐个验证。"""
    import pkgutil

    import frontend.pages as pages_pkg

    names = sorted(m.name for m in pkgutil.iter_modules(pages_pkg.__path__)
                   if not m.name.startswith("_"))
    assert names, "未发现任何页面模块"

    failed = []
    for n in names:
        try:
            _import_page(n).render()
        except Exception as exc:
            failed.append(f"{n}: {type(exc).__name__}: {exc}")
    assert not failed, "页面构建失败:\n  " + "\n  ".join(failed)


def test_daily_signal_snapshot_reads_path_entries(tmp_path, ui_stubs, monkeypatch):
    """信号目录非空时也必须能构建日期快照，防止把 Path 当字符串调用。"""
    import common

    (tmp_path / "signal_20260830.md").write_text("旧信号", encoding="utf-8")
    (tmp_path / "signal_20260831.md").write_text("最新信号", encoding="utf-8")

    mod = _import_page("daily_signal")
    monkeypatch.setattr(common, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)

    choices, latest, content = mod._snapshot()

    assert choices == ["20260830", "20260831"]
    assert latest == "20260831"
    assert content == "最新信号"


def test_operations_full_refresh_requires_confirmation(ui_stubs, monkeypatch):
    mod = _import_page("operations")
    submitted = []

    def fake_stream(task_id, extra_args, title):
        submitted.append((task_id, extra_args, title))
        yield "已提交"

    monkeypatch.setattr(mod, "_stream_operation", fake_stream)

    rejected = list(mod._run_refresh("all", "000300", "20190101", None, 8, False, True, False))
    assert rejected == ["❌ 全量刷新会重拉并覆盖所选股票历史，请先勾选确认。"]
    assert submitted == []

    accepted = list(mod._run_refresh("all", "000300", "20190101", None, 8, False, True, True))
    assert accepted == ["已提交"]
    assert submitted == [
        (
            "refresh",
            [
                "--universe", "all",
                "--index", "000300",
                "--start", "20190101",
                "--workers", "8",
                "--full",
            ],
            "全量数据刷新",
        )
    ]


# ---------------------------------------------------------------- 组件


def test_artifacts_panel_degrades_when_store_missing(tmp_path, ui_stubs, monkeypatch):
    """制品目录不存在时面板不能崩——这是最常见的首次运行状态。"""
    import api.artifacts_api
    from quart.data.artifacts import ArtifactStore

    monkeypatch.setattr(api.artifacts_api, "_store",
                        lambda: ArtifactStore(root=tmp_path / "nope"))

    from frontend.components.artifacts_panel import render_artifacts_panel, render_wfa_panel
    render_artifacts_panel()
    render_wfa_panel()   # 不应抛异常


def test_artifacts_panel_renders_with_data(tmp_path, ui_stubs, monkeypatch):
    import api.artifacts_api
    from quart.data.artifacts import ArtifactStore

    store = ArtifactStore(root=tmp_path / "af")
    monkeypatch.setattr(api.artifacts_api, "_store", lambda: store)

    r = store.create_run("backtest_x", {"top_k": 20}, with_data_version=False)
    r.put_table("equity", __import__("pandas").DataFrame({"equity": [1.0, 1.1]}))
    r.add_metrics(cagr=0.07)
    r.finish()

    from frontend.components.artifacts_panel import render_artifacts_panel
    render_artifacts_panel()
    assert ui_stubs.Accordion.called


def test_presentation_helpers_are_pure():
    """展示层格式化必须在 api 层（可测），不能在 frontend 里（依赖 gradio）。"""
    import api.artifacts_api as A

    for fn in ("runs_table", "run_detail_md", "wfa_panel_md",
               "run_choices", "run_id_from_choice"):
        assert callable(getattr(A, fn)), f"api.artifacts_api 缺少 {fn}"

    import inspect

    import frontend.components.artifacts_panel as P
    src = inspect.getsource(P)
    # 前端组件不应自己拼 Markdown 正文
    assert "**衰减比" not in src, "过拟合诊断文案不应写在 UI 层"
