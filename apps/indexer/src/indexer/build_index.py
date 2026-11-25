"""GOP 索引を作る（build-index）。

raw ゾーンの動画入り MCAP（metadata.json の schema_version が 2 以上で video がある）ごとに:
1. footer と summary だけを Range GET で読む（ファイルの本体は読まない）
2. summary のチャンクの索引のうち、動画のチャンネルを含むものを「GOP 1 つ」として 1 行にする
   （生成器が GOP の先頭でチャンクを区切っているので、1 チャンク = 1 GOP）
3. 予定ルート（/navigation/route）が入っているチャンクだけを Range GET して、ルートの点を取り出す
4. 確認のため、いくつかの GOP のチャンクを Range GET して、先頭が VPS/SPS/PPS 付きの IDR か確かめる
5. 読んだバイト数（summary / ルート / 確認）を MCAP 全体のサイズと並べてログに出す

出力（derived ゾーン）:
    index/gops/device=<id>/date=<YYYY-MM-DD>/<session_id>.parquet     GOP ごとの「MCAP 参照位置」
    index/routes/device=<id>/date=<YYYY-MM-DD>/<session_id>.parquet   予定ルートの点
    index/_manifests/<session_id>.json                                 索引を作った印（MCAP の sha256・読んだバイト数）
同じ MCAP（sha256）・同じ索引作成の版なら、作り直さずにスキップする（冪等）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from indexer import INDEXER_NAME, INDEXER_VERSION
from indexer.mcap_chunks import ROUTE_TOPIC, VIDEO_TOPIC, McapSummary, message_class, nal_unit_types, parse_chunk, read_summary
from indexer.rangefile import RangeReader, ReadStats
from indexer.storage import Location, list_files, open_location, read_bytes, read_json, write_bytes, write_json

log = logging.getLogger(__name__)

_PARTITION_RE = re.compile(r"device=(?P<device>[^/]+)/date=(?P<date>[^/]+)/session_id=(?P<session>[^/]+)/metadata\.json$")

GOP_SCHEMA = pa.schema(
    [
        ("device_id", pa.string()),
        ("date", pa.string()),
        ("session_id", pa.string()),
        ("segment_id", pa.string()),
        ("gop_index", pa.int32()),
        ("log_time_ns", pa.int64()),  # GOP の先頭（IDR）の時刻 = チャンクの最初のメッセージの時刻
        ("end_time_ns", pa.int64()),  # チャンクの最後のメッセージの時刻
        ("frames", pa.int16()),  # GOP のフレーム数
        ("mcap_uri", pa.string()),
        ("chunk_offset", pa.int64()),  # MCAP 参照位置: チャンクの開始オフセット
        ("chunk_length", pa.int64()),  # MCAP 参照位置: チャンクの長さ（この範囲を Range GET すればよい）
        ("chunk_uncompressed_size", pa.int64()),
        ("mcap_size_bytes", pa.int64()),
    ]
)
ROUTE_SCHEMA = pa.schema([("session_id", pa.string()), ("seq", pa.int32()), ("latitude", pa.float64()), ("longitude", pa.float64())])


@dataclass
class IndexReport:
    indexed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    not_video: int = 0
    bytes_read: int = 0
    mcap_bytes: int = 0


def _parquet_bytes(rows: list[dict[str, Any]], schema: pa.Schema) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), sink, compression="zstd")
    return sink.getvalue().to_pybytes()


def _gop_rows(meta: dict[str, Any], summary: McapSummary, device: str, date: str, mcap_uri: str, size: int) -> list[dict[str, Any]]:
    vid = summary.channel_id(VIDEO_TOPIC)
    if vid is None:
        raise ValueError(f"{VIDEO_TOPIC} のチャンネルがありません")
    seg_ns = meta["segments"]["length_s"] * 1_000_000_000
    gop_len = meta["video"]["gop_length"]
    rows = []
    for ci in summary.chunk_indexes:
        if vid not in ci.message_index_offsets:
            continue  # 動画を含まないチャンク（このリポジトリの生成器では起きない）
        gop_index = len(rows)
        rows.append(
            {
                "device_id": device,
                "date": date,
                "session_id": meta["session_id"],
                "segment_id": f"{meta['session_id']}_{(ci.message_start_time - meta['start_time_ns']) // seg_ns:03d}",
                "gop_index": gop_index,
                "log_time_ns": ci.message_start_time,
                "end_time_ns": ci.message_end_time,
                "frames": min(gop_len, meta["video"]["rate_hz"] * meta["duration_s"] - gop_index * gop_len),
                "mcap_uri": mcap_uri,
                "chunk_offset": ci.chunk_start_offset,
                "chunk_length": ci.chunk_length,
                "chunk_uncompressed_size": ci.uncompressed_size,
                "mcap_size_bytes": size,
            }
        )
    return rows


def _read_route(reader: RangeReader, summary: McapSummary) -> list[tuple[float, float]]:
    """予定ルートが入っているチャンクだけを読む（このリポジトリでは先頭の 1 チャンク）。"""
    rid = summary.channel_id(ROUTE_TOPIC)
    if rid is None:
        return []
    _, schema_name = summary.channels[rid]
    cls = message_class(schema_name, summary.schemas[schema_name])
    for ci in summary.chunk_indexes:
        if rid in ci.message_index_offsets:
            for m in parse_chunk(reader.read_range(ci.chunk_start_offset, ci.chunk_length)):
                if m.channel_id == rid:
                    return [(f.latitude, f.longitude) for f in cls.FromString(m.data).fixes]
    return []


def _verify_gops(reader: RangeReader, summary: McapSummary, rows: list[dict[str, Any]], count: int) -> int:
    """GOP の参照位置で Range GET したバイト列だけで、GOP が取り出せるか（先頭が IDR か）を確かめる。確かめた数を返す。"""
    vid = summary.channel_id(VIDEO_TOPIC)
    _, schema_name = summary.channels[vid]  # type: ignore[index]
    cls = message_class(schema_name, summary.schemas[schema_name])
    picks = sorted({0, len(rows) // 2, len(rows) - 1})[:count] if rows else []
    for i in picks:
        row = rows[i]
        frames = [cls.FromString(m.data).data for m in parse_chunk(reader.read_range(row["chunk_offset"], row["chunk_length"])) if m.channel_id == vid]
        types = nal_unit_types(frames[0]) if frames else []
        if len(frames) != row["frames"] or types[:3] != [32, 33, 34] or not ({19, 20} & set(types)):
            raise ValueError(f"GOP {i} の参照位置の確認に失敗: フレーム {len(frames)} 個、先頭の NAL {types[:5]}")
    return len(picks)


def index_session(raw: Location, derived: Location, meta_rel: str, verify: int, force: bool, report: IndexReport) -> None:
    m = _PARTITION_RE.search(meta_rel)
    if m is None:
        return
    meta = json.loads(read_bytes(raw, meta_rel))
    if meta.get("schema_version", 1) < 2 or "video" not in meta:
        report.not_video += 1
        return
    device, date, session_id = m["device"], m["date"], m["session"]
    manifest_rel = f"index/_manifests/{session_id}.json"
    existing = read_json(derived, manifest_rel)
    if not force and existing and existing["mcap_sha256"] == meta["file"]["sha256"] and existing["indexer"]["version"] == INDEXER_VERSION:
        report.skipped.append(session_id)
        return

    mcap_uri = raw.uri_of(meta_rel.rsplit("/", 1)[0] + "/" + meta["file"]["name"])
    stats = ReadStats()
    reader = RangeReader(mcap_uri, stats)
    try:
        summary = read_summary(reader)
        rows = _gop_rows(meta, summary, device, date, mcap_uri, reader.size)
        route_before, _ = stats.snapshot()
        route = _read_route(reader, summary)
        route_bytes = stats.snapshot()[0] - route_before
        verified = _verify_gops(reader, summary, rows, verify)
    finally:
        reader.close()
    total, requests = stats.snapshot()

    part = f"device={device}/date={date}"
    gops_rel, route_rel = f"index/gops/{part}/{session_id}.parquet", f"index/routes/{part}/{session_id}.parquet"
    write_bytes(derived, gops_rel, _parquet_bytes(rows, GOP_SCHEMA))
    write_bytes(derived, route_rel, _parquet_bytes([{"session_id": session_id, "seq": i, "latitude": la, "longitude": lo} for i, (la, lo) in enumerate(route)], ROUTE_SCHEMA))
    write_json(
        derived,
        manifest_rel,
        {
            "session_id": session_id,
            "indexed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "indexer": {"name": INDEXER_NAME, "version": INDEXER_VERSION},
            "mcap": mcap_uri,
            "mcap_sha256": meta["file"]["sha256"],
            "mcap_size_bytes": reader.size,
            "gops": len(rows),
            "route_points": len(route),
            "gops_uri": derived.uri_of(gops_rel),
            "route_uri": derived.uri_of(route_rel),
            "bytes_read": {"summary": summary.summary_bytes_read, "route": route_bytes, "verify": total - summary.summary_bytes_read - route_bytes, "total": total, "requests": requests},
        },
    )
    report.indexed.append(session_id)
    report.bytes_read += total
    report.mcap_bytes += reader.size
    log.info(
        "%s: GOP %d 個・ルート %d 点。読んだバイト数 %d（summary %d / ルート %d / 確認 %d GOP）= MCAP 全体 %d の %.1f%%",
        session_id, len(rows), len(route), total, summary.summary_bytes_read, route_bytes, verified, reader.size, 100.0 * total / reader.size,
    )


def build_index(raw_uri: str, derived_uri: str, verify: int = 1, force: bool = False) -> IndexReport:
    raw, derived = open_location(raw_uri), open_location(derived_uri)
    report = IndexReport()
    for meta_rel in list_files(raw, "", suffix="/metadata.json"):
        index_session(raw, derived, meta_rel, verify, force, report)
    log.info(
        "索引: 作成 %d / スキップ %d / 動画なしのセッション %d。読んだバイト数 %d（索引を作った MCAP の合計 %d の %.2f%%）",
        len(report.indexed), len(report.skipped), report.not_video, report.bytes_read, report.mcap_bytes,
        100.0 * report.bytes_read / report.mcap_bytes if report.mcap_bytes else 0.0,
    )
    return report


