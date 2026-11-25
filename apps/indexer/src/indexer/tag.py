"""セグメントのタグ付け（tag）。

フレームごとの正解値（raw の ground_truth.parquet）を、DuckDB の SQL（sql/tag_segments.sql）で
セグメント（10 秒）ごとに集計し、タグを付けて derived/tags/segments.parquet に書く。
集計の決まり（最大値 / 平均値 / 1 回でも写ったか）は SQL ファイルのコメントを参照。

フレームごとの正解値はそのまま raw に残す（集計で失われる「どのフレームか」が後で必要になったときのため）。
毎回全セッションから作り直す（入力が同じなら結果も同じ。行は segment_id 順に並べる）。
"""

from __future__ import annotations

import logging
from importlib.resources import files

import pyarrow as pa
import pyarrow.parquet as pq

from indexer.duck import connect, create_views
from indexer.storage import Location, write_bytes

log = logging.getLogger(__name__)

SEGMENTS_REL = "tags/segments.parquet"


def load_sql(name: str) -> str:
    """パッケージに同梱した sql/<name>.sql を読む。"""
    return files("indexer").joinpath("sql", f"{name}.sql").read_text(encoding="utf-8")


def tag_segments(raw: Location, derived: Location, raw_uri: str, derived_uri: str) -> pa.Table:
    con = connect(use_s3=raw_uri.startswith("s3://") or derived_uri.startswith("s3://"))
    create_views(con, raw_uri, derived_uri)
    table = con.execute(load_sql("tag_segments")).to_arrow_table()
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    write_bytes(derived, SEGMENTS_REL, sink.getvalue().to_pybytes())

    # タグごとのセグメント数をログに出す（どんなデータがどれだけあるかの目安）
    con.register("tagged", table)
    counts = con.execute("SELECT tag, count(*) AS n FROM (SELECT unnest(tags) AS tag FROM tagged) GROUP BY tag ORDER BY tag").fetchall()
    log.info("タグ付け: セグメント %d 個 → %s", table.num_rows, derived.uri_of(SEGMENTS_REL))
    for tag, n in counts:
        log.info("  %-20s %d", tag, n)
    return table
