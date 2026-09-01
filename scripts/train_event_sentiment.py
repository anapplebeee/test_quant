"""事件情绪模型：时间切分训练 + 置信度保留（RESEARCH-002 §8-4）。

设计（诚实口径）：
- **弱监督**：训练标签来自 ``announcements.rule_sentiment`` 的规则标签，
  模型学到的是"标题措辞 → 规则方向"的一致性外推，不是人工标注情绪；
- **时间切分**：``TimeSeriesSplit`` 按发布时间递增切折，报告逐折 macro-F1，
  禁止随机打散（未来标题不得参与过去训练）；
- **置信度**：predict_proba 最大值作为每条预测的置信度写回事件表，
  ``model_sentiment``（模型）与 ``rule_sentiment``（规则）两列并存，
  sentiment 列切换为模型预测（低置信度回退规则标签）。

用法：
    uv run python scripts/train_event_sentiment.py --events data/events/news.parquet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import data_root

MODEL_PATH = Path(data_root()) / "meta" / "event_sentiment_model.joblib"


def train_time_split(titles: list[str], labels: np.ndarray, published: pd.Series,
                     n_splits: int = 5) -> tuple[object, dict]:
    """时间切分交叉验证 + 全量重训，返回 (模型, 报告)。"""
    order = np.argsort(pd.to_datetime(published).to_numpy())
    titles = [titles[i] for i in order]
    labels = labels[order]
    cv = TimeSeriesSplit(n_splits=n_splits)
    fold_scores = []
    for fold, (tr, te) in enumerate(cv.split(titles), 1):
        pipe = make_pipeline(
            TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3, max_features=60_000),
            LogisticRegression(max_iter=1000, C=1.0),
        )
        pipe.fit([titles[i] for i in tr], labels[tr])
        pred = pipe.predict([titles[i] for i in te])
        score = f1_score(labels[te], pred, average="macro", zero_division=0)
        fold_scores.append({"fold": fold, "n_train": int(len(tr)), "n_test": int(len(te)),
                            "macro_f1": float(score)})
        logger.info("fold {}: train={} test={} macro_f1={:.3f}", fold, len(tr), len(te), score)
    model = make_pipeline(
        TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3, max_features=60_000),
        LogisticRegression(max_iter=1000, C=1.0),
    )
    model.fit(titles, labels)
    report = {"folds": fold_scores, "mean_macro_f1": float(np.mean([f["macro_f1"] for f in fold_scores]))}
    return model, report


def predict_with_confidence(model: object, titles: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """批量预测，返回 (sentiment, confidence=最大类概率)。"""
    proba = model.predict_proba(titles)
    classes = model.classes_
    idx = proba.argmax(axis=1)
    sentiment = classes[idx]
    confidence = proba.max(axis=1)
    return sentiment.astype(float), confidence


def main() -> None:
    parser = argparse.ArgumentParser(description="事件情绪模型（时间切分训练）")
    parser.add_argument("--events", default=str(Path(data_root()) / "events" / "news.parquet"))
    parser.add_argument("--min-confidence", type=float, default=0.55,
                        help="低于该置信度回退规则标签")
    args = parser.parse_args()

    import joblib

    path = Path(args.events)
    if not path.exists():
        raise SystemExit(f"事件文件缺失: {path}（先运行 scripts/fetch_announcements.py）")
    events = pd.read_parquet(path)
    labeled = events[events["rule_sentiment"] != 0]
    if len(labeled) < 2000:
        raise SystemExit(f"有标签事件过少（{len(labeled)}），先扩抓取区间")

    model, report = train_time_split(
        labeled["title"].tolist(),
        labeled["rule_sentiment"].to_numpy(dtype=float),
        labeled["published_at"],
    )
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "report": report}, MODEL_PATH)

    sentiment, confidence = predict_with_confidence(model, events["title"].tolist())
    events["model_sentiment"] = sentiment
    events["model_confidence"] = confidence
    rule = events["rule_sentiment"].to_numpy(dtype=float)
    use_model = (events["model_confidence"].to_numpy() >= args.min_confidence)
    events["sentiment"] = np.where(use_model, events["model_sentiment"], rule)
    events["sentiment_source"] = np.where(use_model, "model", "rule_fallback")
    tmp = path.with_suffix(".tmp")
    events.to_parquet(tmp, index=False)
    tmp.replace(path)
    logger.info("model saved -> {}", MODEL_PATH)
    logger.info("time-split macro_f1: {:.3f} | sentiment sources: {}",
                report["mean_macro_f1"], events["sentiment_source"].value_counts().to_dict())


if __name__ == "__main__":
    main()
