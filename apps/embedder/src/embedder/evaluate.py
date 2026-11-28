"""検索の評価（embedder eval）: テキストのクエリごとに precision@k を測る。

precision@k: 上位 k 件のうち、正解（正解値の条件を満たすセグメント）だった割合。
例: "night" で上位 10 件のうち 9 件が本当に夜のセグメントなら 0.9。
比べる目安として「でたらめに k 件選んだときの期待値」（全セグメントのうち正解の割合 = base rate）も並べる。

クエリと正解の条件の組（DEFAULT_QUERIES）は、合成シーンに写っているものに合わせてある:
    時間帯（day / dusk / night）、天候（clear / rain）、近くの小物体（細い柱）、進路上の大物体（箱）
正解の条件は segments 表の列への SQL（DuckDB で評価する）。--query / --truth で自分の組を足せる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import duckdb

from embedder.search import LEVELS, Query, Searcher, SearchParams, query_vector

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvalCase:
    text: str
    truth: str  # 正解の条件（segments 表の列への SQL の WHERE 句）


# 物体のクエリは、描いているものをそのまま言葉にした（小物体 = 円柱、大物体 = 立方体）。
# 精度が上がる言い回しを探して選ぶことはしない（評価用のデータに合わせ込むことになるため）
DEFAULT_QUERIES = (
    EvalCase("night", "time_of_day = 'night'"),
    EvalCase("dusk", "time_of_day = 'dusk'"),
    EvalCase("rain", "weather = 'rain'"),
    EvalCase("a clear sunny day", "time_of_day = 'day' AND weather = 'clear'"),
    EvalCase("small cylinders nearby", "small_objects_near_any"),
    EvalCase("a large cube ahead", "large_objects_ahead_any"),
)


def _positives(searcher: Searcher, truth: str) -> tuple[set[str], int]:
    """正解の条件を満たすセグメントの集合と、全セグメント数。"""
    seg = searcher.segments.to_arrow().drop_columns(["vector"])  # noqa: F841（DuckDB が変数名で表として読む）
    ids = {r[0] for r in duckdb.sql(f"SELECT segment_id FROM seg WHERE {truth}").fetchall()}
    return ids, seg.num_rows


def evaluate(searcher: Searcher, cases: list[EvalCase], k: int = 10) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        positives, total = _positives(searcher, case.truth)
        query = Query(text=case.text)
        vector = query_vector(searcher.embedder, query)
        row: dict[str, Any] = {"query": case.text, "truth": case.truth, "positives": len(positives), "segments": total, "base_rate": len(positives) / total if total else 0.0}
        for level in LEVELS:
            results = searcher.search(query, SearchParams(top_k=k, level=level), vector=vector)
            hits = sum(1 for r in results if r["segment_id"] in positives)
            row[f"precision_{level}"] = hits / k
            row[f"hits_{level}"] = hits
            row[f"top_score_{level}"] = results[0]["score"] if results else None
        rows.append(row)
        log.info("%-22s P@%d segments=%.2f frames=%.2f（正解 %d / %d、でたらめなら %.2f）",
                 case.text, k, row["precision_segments"], row["precision_frames"], len(positives), total, row["base_rate"])
    return rows


def format_table(rows: list[dict[str, Any]], k: int) -> str:
    lines = [
        f"| クエリ | 正解の条件 | P@{k}（セグメント平均） | P@{k}（キーフレーム最大） | 正解の数 / 全体 | でたらめなら |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['query']} | `{r['truth']}` | {r['precision_segments']:.2f} | {r['precision_frames']:.2f} | {r['positives']} / {r['segments']} | {r['base_rate']:.2f} |"
        )
    return "\n".join(lines)
