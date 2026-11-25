"""正解値との照合（count-truth）。

選別の結果（DuckDB の SQL）が正しいかを、別の情報源で数え直して確かめる。
ここでは raw の metadata.json（生成器が書いたセッションの条件とセグメント数）だけを使い、
DuckDB も Parquet も通さずに「条件に合うセグメントの数」を数える。
"""

from __future__ import annotations

import json
import logging

from indexer.storage import Location, list_files, read_bytes

log = logging.getLogger(__name__)


def count_segments(raw: Location, time_of_day: str | None, weather: str | None) -> tuple[int, int]:
    """条件に合うセッションのセグメント数の合計と、動画入りのセグメントの総数を返す。"""
    matched = total = 0
    for rel in list_files(raw, "", suffix="/metadata.json"):
        meta = json.loads(read_bytes(raw, rel))
        if "segments" not in meta:
            continue  # 動画なしのセッション
        n = meta["segments"]["count"]
        total += n
        cond = meta["conditions"]
        if (time_of_day is None or cond["time_of_day"] == time_of_day) and (weather is None or cond["weather"] == weather):
            matched += n
    log.info("metadata.json から数えた正解: 条件に合うセグメント %d 個 / 動画入りのセグメント %d 個", matched, total)
    return matched, total
