"""产出制品仓库（ArtifactStore）。

为什么需要它
------------
此前 scripts → api → frontend 的**唯一**通信机制是 `reports/` 下的
csv/json/md 文件，靠 `glob` + 文件 mtime 猜"哪些是新产出"：

    files = sorted(_glob.glob("reports/summary_*.csv"), key=os.path.getmtime)
    new_files = [f for f in files if mtime(f) >= since]

这套机制没有 run_id、没有 schema、没有参数记录、没有数据版本。后果：
  * 无法回答"这个 CAGR 是哪次运行、用哪套参数、跑在哪份数据上产生的"
  * 文件名冲突、部分写入、并发任务互相覆盖都无法检测
  * 结论不可复现——README 里作废过三轮数字，根因之一就是追溯不了

本模块提供显式契约：

    artifacts/
    └── backtest_20260830_201530_a1b2c3d4/
        ├── manifest.json      # run_id / 参数 / 数据版本 / 产出清单 / 指标
        ├── equity.parquet
        ├── trades.parquet
        └── summary.json

迁移策略：scripts 同时写 `reports/`（兼容现有 api/frontend）与
`artifacts/`（可追溯），前端逐步改按 run_id 查询后即可停写 reports/。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quart.config import PROJECT_ROOT

#: 制品仓库根目录
ARTIFACTS_DIRNAME = "artifacts"

#: manifest 文件名
MANIFEST_NAME = "manifest.json"

#: 状态
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"


def artifacts_root() -> Path:
    return PROJECT_ROOT / ARTIFACTS_DIRNAME


def _short_hash(payload: str, n: int = 12) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:n]


def _git_revision() -> str:
    """当前代码版本。非 git 环境或失败时返回 'unknown'。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _global_data_dates(store) -> tuple[str | None, str | None]:
    """全市场最早/最新交易日（不依赖单只票）。

    2026-08-31 审查修复：旧实现只取 `symbols[0]/symbols[-1]` 两只票的日期，
    其余股票更新（退市回填、数据修正）不改变指纹，可复现性契约失效。
    分区布局下只扫最小/最大年份目录，开销可控。
    """
    if store._partitioned:
        years = store._partition_years(store.daily_dir)
        if not years:
            return None, None
        last: pd.Timestamp | None = None
        first: pd.Timestamp | None = None
        for p in (store.daily_dir / f"year={max(years)}").glob("*.parquet"):
            try:
                d = pd.read_parquet(p, columns=["date"])["date"]
            except Exception:
                continue
            if not d.empty:
                m = d.max()
                last = m if (last is None or m > last) else last
        for p in (store.daily_dir / f"year={min(years)}").glob("*.parquet"):
            try:
                d = pd.read_parquet(p, columns=["date"])["date"]
            except Exception:
                continue
            if not d.empty:
                m = d.min()
                first = m if (first is None or m < first) else first
        return (
            str(last.date()) if last is not None else None,
            str(first.date()) if first is not None else None,
        )
    # 旧平铺布局：遍历全部代码（数据量小，直接读 date 列）
    last = first = None
    for sym in store.symbols():
        try:
            d = pd.read_parquet(store.daily_dir / f"{sym}.parquet", columns=["date"])["date"]
        except Exception:
            continue
        if d.empty:
            continue
        mx, mn = d.max(), d.min()
        last = mx if (last is None or mx > last) else last
        first = mn if (first is None or mn < first) else first
    return (
        str(last.date()) if last is not None else None,
        str(first.date()) if first is not None else None,
    )


def data_version(store=None) -> dict:
    """数据版本指纹：股票数 + 全市场最新日期 + 最早日期。

    结果数字必须能回答"跑在哪份数据上"——数据变了，
    fingerprint 就变，旧结论自动失效而不必靠人工记忆。
    """
    try:
        if store is None:
            from quart.data.store import BarStore

            store = BarStore()
        symbols = store.symbols()
        if not symbols:
            return {"symbols": 0, "last_date": None, "first_date": None}
        last_date, first_date = _global_data_dates(store)
        return {
            "symbols": len(symbols),
            "last_date": last_date,
            "first_date": first_date,
        }
    except Exception:
        return {"symbols": 0, "last_date": None, "first_date": None}


def fingerprint(params: dict, data: dict | None = None, code: str = "unknown") -> str:
    """结果指纹：参数 + 数据版本 + 代码版本 → 短哈希。

    同指纹 = 同输入，用于判断两次运行是否可比。
    """
    payload = json.dumps(
        {"params": _canon(params), "data": data or {}, "code": code},
        sort_keys=True, default=str,
    )
    return _short_hash(payload)


def _canon(obj: Any) -> Any:
    """JSON 规范化（含 numpy/pandas 标量与 Timestamp）。"""
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy 标量
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


@dataclass
class Artifact:
    """单个产出文件。"""

    name: str
    relpath: str
    kind: str                       # table | curve | text | json
    rows: int | None = None
    bytes: int = 0


@dataclass
class Manifest:
    """一次运行的完整记录。"""

    run_id: str
    task: str
    created_at: str
    status: str = STATUS_RUNNING
    params: dict = field(default_factory=dict)
    data_version: dict = field(default_factory=dict)
    code: str = "unknown"
    fingerprint: str = ""
    metrics: dict = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None

    def artifact(self, name: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.name == name), None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            **{**d, "artifacts": [Artifact(**a) for a in d.get("artifacts", [])]}
        )


class RunWriter:
    """一次运行的写入句柄。用 `store.create_run()` 获取，不要直接构造。"""

    def __init__(self, store: "ArtifactStore", manifest: Manifest):
        self._store = store
        self.manifest = manifest
        self.dir = store.root / manifest.run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 写入 ----------------

    def put_table(self, name: str, df: pd.DataFrame, index: bool = False) -> Artifact:
        """写 Parquet 表（equity / trades / sweep 等）。"""
        path = self.dir / f"{name}.parquet"
        df.to_parquet(path, index=index)
        return self._register(name, path, "table", rows=len(df))

    def put_json(self, name: str, obj: Any) -> Artifact:
        path = self.dir / f"{name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_canon(obj), f, ensure_ascii=False, indent=2, default=str)
        return self._register(name, path, "json")

    def put_text(self, name: str, content: str) -> Artifact:
        path = self.dir / f"{name}.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return self._register(name, path, "text")

    def add_metrics(self, **kwargs) -> None:
        """补充指标（如 cagr / sharpe / max_drawdown）。"""
        self.manifest.metrics.update(_canon(kwargs))

    def finish(self, status: str = STATUS_OK, error: str | None = None) -> Manifest:
        self.manifest.status = status
        self.manifest.error = error
        self._store._write_manifest(self.manifest)
        return self.manifest

    def _register(self, name: str, path: Path, kind: str, rows: int | None = None) -> Artifact:
        art = Artifact(
            name=name,
            relpath=path.relative_to(self._store.root).as_posix(),
            kind=kind,
            rows=rows,
            bytes=path.stat().st_size,
        )
        self.manifest.artifacts = [
            a for a in self.manifest.artifacts if a.name != name
        ] + [art]
        self._store._write_manifest(self.manifest)
        return art


class ArtifactStore:
    """制品仓库。"""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else artifacts_root()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------- 写入 ----------------

    def create_run(
        self,
        task: str,
        params: dict | None = None,
        run_id: str | None = None,
        with_data_version: bool = True,
    ) -> RunWriter:
        """开启一次运行。

        Parameters
        ----------
        with_data_version:
            是否采集数据版本。离线测试/单测里可以关掉以提速。
        """
        params = params or {}
        now = datetime.now()
        data = data_version() if with_data_version else {}
        code = _git_revision()
        # run_id 含微秒：同一秒内连续创建时，字典序仍等于创建顺序，
        # 使 latest()/prune() 的行为可预期（哈希后缀只用于避免极端碰撞）
        rid = run_id or (
            f"{task}_{now.strftime('%Y%m%d_%H%M%S_%f')}_{_short_hash(str(now.timestamp()) + task, 6)}"
        )
        manifest = Manifest(
            run_id=rid,
            task=task,
            created_at=now.isoformat(),
            params=_canon(params),
            data_version=data,
            code=code,
            fingerprint=fingerprint(params, data, code),
        )
        self._write_manifest(manifest)
        return RunWriter(self, manifest)

    def _write_manifest(self, manifest: Manifest) -> None:
        path = self.root / manifest.run_id / MANIFEST_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(path)

    # ---------------- 查询 ----------------

    def list_runs(self, task: str | None = None, status: str | None = None) -> list[Manifest]:
        out: list[Manifest] = []
        for path in sorted(self.root.glob(f"*/{MANIFEST_NAME}")):
            try:
                with open(path, encoding="utf-8") as f:
                    m = Manifest.from_dict(json.load(f))
            except Exception:
                continue
            if task and m.task != task:
                continue
            if status and m.status != status:
                continue
            out.append(m)
        # 二级排序键用 run_id：created_at 只到秒，同一秒内创建的多个 run
        # 若只按时间排序，顺序会随文件名（含随机哈希）抖动，
        # 导致 latest() 与 prune() 选错对象。
        out.sort(key=lambda m: (m.created_at, m.run_id), reverse=True)
        return out

    def latest(self, task: str | None = None, status: str = STATUS_OK) -> Manifest | None:
        runs = self.list_runs(task=task, status=status)
        return runs[0] if runs else None

    def load_manifest(self, run_id: str) -> Manifest | None:
        path = self.root / run_id / MANIFEST_NAME
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return Manifest.from_dict(json.load(f))
        except Exception:
            return None

    def read(self, run_id: str, name: str) -> pd.DataFrame | None:
        """读取表/曲线制品。"""
        m = self.load_manifest(run_id)
        if m is None:
            return None
        art = m.artifact(name)
        if art is None:
            return None
        path = self.root / art.relpath
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def read_text(self, run_id: str, name: str) -> str | None:
        m = self.load_manifest(run_id)
        if m is None:
            return None
        art = m.artifact(name)
        if art is None:
            return None
        path = self.root / art.relpath
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def path_of(self, run_id: str, name: str) -> Path | None:
        m = self.load_manifest(run_id)
        if m is None:
            return None
        art = m.artifact(name)
        return (self.root / art.relpath) if art else None

    # ---------------- 维护 ----------------

    def prune(self, keep_last: int = 100, task: str | None = None) -> int:
        """保留最近 N 次运行，删除其余目录。返回删除数量。"""
        runs = self.list_runs(task=task)
        doomed = runs[keep_last:]
        n = 0
        for m in doomed:
            d = self.root / m.run_id
            try:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
                n += 1
            except Exception:
                continue
        return n


__all__ = [
    "ARTIFACTS_DIRNAME",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_RUNNING",
    "Artifact",
    "ArtifactStore",
    "Manifest",
    "RunWriter",
    "artifacts_root",
    "data_version",
    "fingerprint",
]
