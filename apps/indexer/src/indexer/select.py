"""選別とサンプル一覧（manifest）の作成（select）。

1. 選別の SQL（sql/<名前>.sql、またはファイルのパス）を DuckDB で実行し、segment_id の一覧を得る
   - SQL から使える表: segments（タグと集計値）、gops（GOP 索引）、frames（フレームごとの正解値）
   - --segment-ids-uri を付けると、外から渡した segment_id の一覧（Parquet、segment_id 列）を
     candidates という表として使える（フェーズ 6 の検索結果をここに渡して、タグの条件と組み合わせる）
2. 選ばれたセグメントの GOP（gops）とフレームの正解値（frames）を結び付け、
   1 サンプル = segment_id + GOP（MCAP 参照位置）+ GOP 内のフレーム番号 + 正解値 にする
   （予定ルートの Parquet の場所 route_uri も付ける。学習器が経路ラスタを描くのに使う）
   - --frames first … 各 GOP の先頭フレーム（IDR だけ。デコードが一番軽い）
   - --frames random … 各 GOP から 1 フレーム（乱数の種で決まる。同じ種なら毎回同じ）
   - --frames all … GOP の全フレーム
3. MCAP の参照位置の順に並べ、GOP の数でシャードに分けて curated に書く:
       curated/<dataset>/v<version>/manifest.json            選別の条件（SQL の本文）・件数・入力のハッシュ
       curated/<dataset>/v<version>/segments.parquet         選ばれた segment_id
       curated/<dataset>/v<version>/shards/shard-00000.parquet … サンプルの一覧
   同じバージョンが既にあり、中身（ハッシュ）が同じならスキップ、違えばエラー（バージョンは書き換えない）
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from indexer import INDEXER_NAME, INDEXER_VERSION
from indexer.duck import _sql_str, connect, create_segments_view, create_views
from indexer.storage import Location, read_json, write_bytes, write_json
from indexer.tag import load_sql

log = logging.getLogger(__name__)

FRAME_CHOICES = ("first", "random", "all")

# サンプル 1 件の列。学習器（apps/trainer の video_dataset）がこの列を読む
SAMPLE_COLUMNS = """
    g.segment_id, g.session_id, g.device_id, g.gop_index, f.frame_in_gop, f.frame_index,
    f.log_time_ns, g.log_time_ns AS gop_log_time_ns,
    g.mcap_uri, g.chunk_offset, g.chunk_length, g.frames AS gop_frames, g.mcap_size_bytes,
    {route_prefix} || '/device=' || g.device_id || '/date=' || g.date || '/' || g.session_id || '.parquet' AS route_uri,
    f.time_of_day, f.weather, f.small_objects_near, f.large_objects_ahead,
    s.small_objects_near_max AS segment_small_objects_near_max,
    s.large_objects_ahead_any AS segment_large_objects_ahead_any
"""


class ManifestConflictError(RuntimeError):
    """同じバージョンに、別の中身の manifest が既にある。"""


@dataclass(frozen=True)
class SelectParams:
    dataset: str
    version: int
    sql: str  # sql/ の名前（例: night_rain）か、.sql ファイルのパス
    frames: str = "first"
    seed: int = 0
    gops_per_shard: int = 50
    segment_ids_uri: str | None = None


def read_selection_sql(sql: str) -> str:
    path = Path(sql)
    return path.read_text(encoding="utf-8") if path.suffix == ".sql" and path.exists() else load_sql(sql)


def _random_frame(segment_id: str, gop_index: int, frames: int, seed: int) -> int:
    """GOP ごとに 1 フレームを決める。sha256 を使うので、実行環境や版が変わっても同じ結果になる。"""
    digest = hashlib.sha256(f"{seed}:{segment_id}:{gop_index}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % frames


def _samples(con: Any, segment_ids: list[str], params: SelectParams, derived_uri: str) -> list[dict[str, Any]]:
    con.execute("CREATE OR REPLACE TEMP TABLE selected AS SELECT unnest(?::VARCHAR[]) AS segment_id", [segment_ids])
    rows = con.execute(
        f"""
        SELECT {SAMPLE_COLUMNS.format(route_prefix=_sql_str(derived_uri + "/index/routes"))}
        FROM gops AS g
        JOIN selected USING (segment_id)
        JOIN frames AS f ON f.session_id = g.session_id AND f.gop_index = g.gop_index
        JOIN segments AS s ON s.segment_id = g.segment_id
        ORDER BY g.mcap_uri, g.chunk_offset, f.frame_in_gop
        """
    ).to_arrow_table().to_pylist()
    if params.frames == "first":
        return [r for r in rows if r["frame_in_gop"] == 0]
    if params.frames == "random":
        return [r for r in rows if r["frame_in_gop"] == _random_frame(r["segment_id"], r["gop_index"], r["gop_frames"], params.seed)]
    return rows


def _shards(samples: list[dict[str, Any]], gops_per_shard: int) -> list[list[dict[str, Any]]]:
    """GOP の切れ目でシャードに分ける（1 つの GOP のサンプルが 2 つのシャードにまたがらないように）。"""
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    gops_in_current, last_key = 0, None
    for s in samples:
        key = (s["mcap_uri"], s["chunk_offset"])
        if key != last_key:
            if gops_in_current == gops_per_shard:
                shards.append(current)
                current, gops_in_current = [], 0
            gops_in_current += 1
            last_key = key
        current.append(s)
    if current:
        shards.append(current)
    return shards


def _parquet(rows: list[dict[str, Any]], schema: pa.Schema | None = None) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), sink, compression="zstd")
    return sink.getvalue().to_pybytes()


def select_and_curate(raw_uri: str, derived_uri: str, curated: Location, params: SelectParams) -> dict[str, Any]:
    if params.frames not in FRAME_CHOICES:
        raise ValueError(f"--frames は {FRAME_CHOICES} のどれか: {params.frames}")
    sql_text = read_selection_sql(params.sql)
    con = connect(use_s3=any(u.startswith("s3://") for u in (raw_uri, derived_uri, params.segment_ids_uri or "")))
    create_views(con, raw_uri, derived_uri)
    create_segments_view(con, derived_uri)
    if params.segment_ids_uri:
        con.execute(f"CREATE OR REPLACE VIEW candidates AS SELECT segment_id FROM read_parquet({_sql_str(params.segment_ids_uri)})")

    segment_ids = sorted({r[0] for r in con.execute(sql_text).fetchall()})
    samples = _samples(con, segment_ids, params, derived_uri)
    shards = _shards(samples, params.gops_per_shard)
    gops = len({(s["mcap_uri"], s["chunk_offset"]) for s in samples})
    mcaps = sorted({s["mcap_uri"] for s in samples})

    # 中身のハッシュ: 選別の条件と、サンプルの一覧（参照位置・正解値）から作る。作成時刻は含めない
    content_hash = hashlib.sha256(
        json.dumps({"sql": sql_text, "params": {"frames": params.frames, "seed": params.seed, "gops_per_shard": params.gops_per_shard}, "samples": samples}, sort_keys=True).encode()
    ).hexdigest()
    base = f"{params.dataset}/v{params.version}"
    existing = read_json(curated, f"{base}/manifest.json")
    if existing is not None:
        if existing["content_hash"] == content_hash:
            log.info("同じ中身の %s が既にあるのでスキップします", curated.uri_of(base))
            return existing
        raise ManifestConflictError(f"{curated.uri_of(base)} は別の中身で作成済みです。--version を上げてください")

    shard_uris = []
    for i, shard in enumerate(shards):
        rel = f"{base}/shards/shard-{i:05d}.parquet"
        write_bytes(curated, rel, _parquet(shard))
        shard_uris.append(curated.uri_of(rel))
    write_bytes(curated, f"{base}/segments.parquet", _parquet([{"segment_id": s} for s in segment_ids], pa.schema([("segment_id", pa.string())])))
    manifest = {
        "dataset": params.dataset,
        "version": params.version,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "indexer": {"name": INDEXER_NAME, "version": INDEXER_VERSION},
        "selection": {"sql_name": params.sql, "sql": sql_text, "segment_ids_uri": params.segment_ids_uri},
        "frames": params.frames,
        "seed": params.seed,
        "counts": {"segments": len(segment_ids), "gops": gops, "samples": len(samples), "shards": len(shards), "mcap_files": len(mcaps)},
        # 学習器が「読み出したバイト数」と比べるための値（選ばれた GOP のチャンクの合計）
        "selected_chunk_bytes": sum({(s["mcap_uri"], s["chunk_offset"]): s["chunk_length"] for s in samples}.values()),
        "selected_mcap_bytes": sum({s["mcap_uri"]: s["mcap_size_bytes"] for s in samples}.values()),
        "shards": shard_uris,
        "content_hash": content_hash,
    }
    write_json(curated, f"{base}/manifest.json", manifest)
    log.info(
        "選別: セグメント %d 個 / GOP %d 個 / サンプル %d 件（--frames %s）→ %s（シャード %d 個）",
        len(segment_ids), gops, len(samples), params.frames, curated.uri_of(base), len(shards),
    )
    return manifest
