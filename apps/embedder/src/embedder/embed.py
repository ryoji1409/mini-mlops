"""埋め込みの生成（embedder embed）。

1. GOP 索引（derived/index/gops）から、キーフレームの一覧を作る（正解値も付ける）
2. セッションごとに、表に入っている行と索引を比べる。同じモデル・リビジョンの表に、同じ GOP（同じ参照位置）が
   全部入っていればスキップする（冪等。再実行しても同じ結果になり、余計な読み出しをしない）
3. スキップしないセッションは、キーフレームを Range GET でデコードし、まとめて（batch_size 枚ずつ）埋め込んで、
   セッション単位で frames 表に書く（途中で止まっても、書き終えたセッションは次回スキップされる）
   - そのセッションの古い行（参照位置が変わった、間引きの間隔が変わった、など）は先に消す
4. frames 表に変化があれば、segments 表（セグメントごとの平均ベクトル）を作り直す

LanceDB の表は、同時に 2 つのジョブから書かない前提（ワークフローの mutex で 1 本ずつにしている）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from indexer.rangefile import ReadStats
from indexer.storage import exists, open_location

from embedder.keyframes import KeyframeReader, iter_keyframes, list_keyframes
from embedder.model import SiglipEmbedder
from embedder.tables import FRAMES, SEGMENTS, build_segments, connect, frame_rows, open_table, table_name

log = logging.getLogger(__name__)


@dataclass
class EmbedReport:
    embedded_sessions: list[str] = field(default_factory=list)
    skipped_sessions: list[str] = field(default_factory=list)
    keyframes: int = 0
    bytes_read: int = 0
    requests: int = 0
    mcap_bytes: int = 0  # 埋め込んだセッションの MCAP の合計サイズ（読んだ量と比べるため）
    frames_table: str = ""
    segments_table: str = ""
    segments: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "embedded_sessions": len(self.embedded_sessions),
            "skipped_sessions": len(self.skipped_sessions),
            "keyframes": self.keyframes,
            "bytes_read": self.bytes_read,
            "requests": self.requests,
            "mcap_bytes": self.mcap_bytes,
            "frames_table": self.frames_table,
            "segments_table": self.segments_table,
            "segments": self.segments,
        }


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _group_by_session(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["session_id"], []).append(r)
    return out


def _existing_keys(table: Any) -> dict[str, set[tuple[str, int, int]]]:
    """表に入っている行の「セッション → (MCAP, GOP 番号, 参照位置) の集合」。スキップの判定に使う。"""
    if table is None:
        return {}
    keys: dict[str, set[tuple[str, int, int]]] = {}
    for r in table.to_arrow().select(["session_id", "mcap_uri", "gop_index", "chunk_offset"]).to_pylist():
        keys.setdefault(r["session_id"], set()).add((r["mcap_uri"], r["gop_index"], r["chunk_offset"]))
    return keys


def _read_tags(derived_uri: str) -> dict[str, dict[str, Any]]:
    """フェーズ 5 のタグ（tags/segments.parquet）を segment_id → 行 にする。無ければ空。"""
    loc = open_location(derived_uri)
    if not exists(loc, "tags/segments.parquet"):
        log.warning("タグ（%s/tags/segments.parquet）が無いので、セグメントの正解値はキーフレームだけで集計します", derived_uri)
        return {}
    with loc.fs.open_input_file(loc.path("tags/segments.parquet")) as f:
        rows = pq.ParquetFile(f).read().to_pylist()
    return {r["segment_id"]: r for r in rows}


def _embed_session(
    embedder: SiglipEmbedder, reader: KeyframeReader, rows: list[dict[str, Any]], batch_size: int, prefetch: int
) -> list[Any]:
    """1 セッション分のキーフレームを読んで、batch_size 枚ずつ埋め込む。"""
    vectors: list[Any] = []
    batch: list[Any] = []
    for _, image in iter_keyframes(reader, rows, prefetch):
        batch.append(image)
        if len(batch) >= batch_size:
            vectors.extend(embedder.embed_images(batch))
            batch = []
    if batch:
        vectors.extend(embedder.embed_images(batch))
    return vectors


def embed(
    raw_uri: str,
    derived_uri: str,
    vectors_uri: str,
    embedder: SiglipEmbedder,
    gop_stride: int = 1,
    batch_size: int = 8,
    prefetch: int = 4,
    force: bool = False,
) -> EmbedReport:
    ref = embedder.ref
    report = EmbedReport(frames_table=table_name(ref, FRAMES), segments_table=table_name(ref, SEGMENTS))
    rows = list_keyframes(raw_uri, derived_uri, gop_stride)
    sessions = _group_by_session(rows)
    log.info("キーフレーム %d 枚（%d セッション、GOP %d 個おき）。モデル %s 版 %s（リビジョン %s）", len(rows), len(sessions), gop_stride, ref.name, ref.version, ref.short_revision)

    db = connect(vectors_uri)
    frames_tbl = open_table(db, report.frames_table)
    existing = _existing_keys(frames_tbl)
    stats = ReadStats()
    reader = KeyframeReader(stats=stats)
    try:
        for sid, srows in sessions.items():
            wanted = {(r["mcap_uri"], r["gop_index"], r["chunk_offset"]) for r in srows}
            if not force and existing.get(sid) == wanted:
                report.skipped_sessions.append(sid)
                continue
            before = stats.snapshot()[0]
            vectors = _embed_session(embedder, reader, srows, batch_size, prefetch)
            table = frame_rows(srows, np.asarray(vectors), ref)
            if frames_tbl is None:
                frames_tbl = db.create_table(report.frames_table, data=table)
            else:
                if sid in existing:
                    frames_tbl.delete(f"session_id = {_sql_str(sid)}")  # 古い行を消してから入れ直す
                frames_tbl.add(table)
            report.embedded_sessions.append(sid)
            report.keyframes += len(srows)
            report.mcap_bytes += srows[0]["mcap_size_bytes"]
            log.info("%s: キーフレーム %d 枚を埋め込みました。読んだバイト数 %d（MCAP 全体 %d の %.1f%%）",
                     sid, len(srows), stats.snapshot()[0] - before, srows[0]["mcap_size_bytes"], 100 * (stats.snapshot()[0] - before) / srows[0]["mcap_size_bytes"])
    finally:
        reader.close()
    report.bytes_read, report.requests = stats.snapshot()

    # 索引から消えたセッション（raw から消した、など）の行も消す
    gone = set(existing) - set(sessions)
    if frames_tbl is not None and gone:
        frames_tbl.delete(" OR ".join(f"session_id = {_sql_str(s)}" for s in sorted(gone)))
        log.info("索引に無いセッション %d 個の行を消しました", len(gone))

    if frames_tbl is None:
        log.warning("埋め込むキーフレームがありません（索引が空？ 先に build-video-index を実行する）")
        return report
    segments_tbl = open_table(db, report.segments_table)
    if report.embedded_sessions or gone or segments_tbl is None or force:
        segments = build_segments(frames_tbl.to_arrow(), ref, _read_tags(derived_uri))
        segments_tbl = db.create_table(report.segments_table, data=segments, mode="overwrite")
        frames_tbl.optimize()  # 小さなファイル（セッションごとの追加）をまとめる
    report.segments = segments_tbl.count_rows()

    log.info(
        "埋め込み: 作成 %d / スキップ %d セッション、キーフレーム %d 枚 → %s（セグメント %d 個 → %s）",
        len(report.embedded_sessions), len(report.skipped_sessions), report.keyframes, report.frames_table, report.segments, report.segments_table,
    )
    if report.mcap_bytes:
        log.info("読んだバイト数 %d（%d 回）= 埋め込んだセッションの MCAP の合計 %d の %.1f%%",
                 report.bytes_read, report.requests, report.mcap_bytes, 100 * report.bytes_read / report.mcap_bytes)
    return report
