"""LanceDB の表（s3://vectors/ に置く）。

LanceDB: ベクトルと普通の列を一緒に持てる表（Lance 形式のファイル）。S3 互換ストレージの上にそのまま置け、
サーバーを立てずに、ライブラリだけで「このベクトルに近い行」を探せる（ベクトル検索）。

表は 2 つ。名前にモデル名とリビジョン（先頭 7 文字）を入れるので、モデルを替えると別の表になる（古い表は残る）:
    <モデル名>__<リビジョン>__frames    … 1 行 = 1 キーフレーム
    <モデル名>__<リビジョン>__segments  … 1 行 = 1 セグメント（10 秒）。キーフレームの埋め込みを平均して、長さ 1 にそろえ直したもの

行数が少ない（数千行）ので、ベクトルの索引（IVF-PQ などの近似索引）は作らず、全件と比べる（その方が正確で、十分速い）。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import lancedb
import numpy as np
import pyarrow as pa

from embedder.keyframes import GT_COLUMNS
from embedder.registry import ModelRef

FRAMES = "frames"
SEGMENTS = "segments"


def table_name(ref: ModelRef, kind: str) -> str:
    return f"{ref.name}__{ref.short_revision}__{kind}"


def storage_options() -> dict[str, str]:
    """aws CLI と同じ環境変数（AWS_ENDPOINT_URL など）から、LanceDB の S3 の設定を作る。"""
    opts = {"aws_region": os.environ.get("AWS_REGION", "us-east-1")}
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        opts.update(aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"])
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint:
        opts["aws_endpoint"] = endpoint
        # SeaweedFS はパス形式（http://host/bucket/key）で使う。http のエンドポイント（クラスタ内）も許す
        opts["aws_virtual_hosted_style_request"] = "false"
        if urlparse(endpoint).scheme == "http":
            opts["allow_http"] = "true"
    return opts


def connect(vectors_uri: str) -> Any:
    if vectors_uri.startswith("s3://"):
        return lancedb.connect(vectors_uri, storage_options=storage_options())
    return lancedb.connect(vectors_uri)


def open_table(db: Any, name: str) -> Any | None:
    try:
        return db.open_table(name)
    except (FileNotFoundError, ValueError):  # まだ無い表
        return None


def _vector_type(dim: int) -> pa.DataType:
    return pa.list_(pa.float32(), dim)  # 長さ固定のリスト（LanceDB はこの型の列をベクトルとして扱う）


def frames_schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            ("segment_id", pa.string()),
            ("session_id", pa.string()),
            ("device_id", pa.string()),
            ("gop_index", pa.int32()),
            ("frame_index", pa.int32()),
            ("log_time_ns", pa.int64()),
            # MCAP の参照位置（この範囲を Range GET すれば、同じキーフレームをもう一度取り出せる）
            ("mcap_uri", pa.string()),
            ("chunk_offset", pa.int64()),
            ("chunk_length", pa.int64()),
            ("vector", _vector_type(dim)),
            ("model", pa.string()),
            ("model_version", pa.string()),
            ("model_revision", pa.string()),
            ("time_of_day", pa.string()),
            ("weather", pa.string()),
            ("small_objects_near", pa.int16()),
            ("large_objects_ahead", pa.int16()),
        ]
    )


def segments_schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            ("segment_id", pa.string()),
            ("session_id", pa.string()),
            ("device_id", pa.string()),
            ("start_time_ns", pa.int64()),
            ("keyframes", pa.int32()),
            ("vector", _vector_type(dim)),
            ("model", pa.string()),
            ("model_version", pa.string()),
            ("model_revision", pa.string()),
            # 正解値（セグメントの集計。tags/segments.parquet と同じ意味）
            ("time_of_day", pa.string()),
            ("weather", pa.string()),
            ("small_objects_near_max", pa.int16()),
            ("small_objects_near_any", pa.bool_()),
            ("large_objects_ahead_any", pa.bool_()),
        ]
    )


def frame_rows(rows: list[dict[str, Any]], vectors: np.ndarray, ref: ModelRef) -> pa.Table:
    """索引の行（正解値付き）と埋め込みから、frames 表に入れる行を作る。"""
    keep = ("segment_id", "session_id", "device_id", "gop_index", "frame_index", "log_time_ns", "mcap_uri", "chunk_offset", "chunk_length", *GT_COLUMNS)
    data: dict[str, Any] = {k: [r[k] for r in rows] for k in keep}
    data["vector"] = list(vectors)
    data |= {"model": [ref.name] * len(rows), "model_version": [ref.version] * len(rows), "model_revision": [ref.hf_revision] * len(rows)}
    schema = frames_schema(vectors.shape[1])
    return pa.table({f.name: pa.array(data[f.name], type=f.type) for f in schema}, schema=schema)


def build_segments(frames: pa.Table, ref: ModelRef, tags: dict[str, dict[str, Any]] | None = None) -> pa.Table:
    """frames 表から、セグメントごとの平均ベクトルの表を作る。

    平均してから長さ 1 にそろえ直す（コサイン類似度で比べるため）。
    セグメントの正解値は、フェーズ 5 のタグ（tags/segments.parquet。全フレームで集計した値）があればそれを使う。
    無ければキーフレームだけで集計する（小物体・大物体は、キーフレームのうち 1 枚でもあれば「あり」）。
    """
    tags = tags or {}
    cols = frames.select(["segment_id", "session_id", "device_id", "log_time_ns", "time_of_day", "weather", "small_objects_near", "large_objects_ahead"]).to_pylist()
    vectors = np.asarray(frames.column("vector").to_pylist(), dtype=np.float32)
    by_segment: dict[str, list[int]] = {}
    for i, r in enumerate(cols):
        by_segment.setdefault(r["segment_id"], []).append(i)
    out: dict[str, list[Any]] = {f.name: [] for f in segments_schema(vectors.shape[1])}
    for seg_id in sorted(by_segment):
        idx = by_segment[seg_id]
        first = cols[idx[0]]
        mean = vectors[idx].mean(axis=0)
        out["segment_id"].append(seg_id)
        out["session_id"].append(first["session_id"])
        out["device_id"].append(first["device_id"])
        out["start_time_ns"].append(min(cols[i]["log_time_ns"] for i in idx))
        out["keyframes"].append(len(idx))
        out["vector"].append(mean / np.linalg.norm(mean))
        out["model"].append(ref.name)
        out["model_version"].append(ref.version)
        out["model_revision"].append(ref.hf_revision)
        out["time_of_day"].append(first["time_of_day"])
        out["weather"].append(first["weather"])
        tag = tags.get(seg_id)
        if tag is not None:
            out["small_objects_near_max"].append(tag["small_objects_near_max"])
            out["small_objects_near_any"].append(tag["small_objects_near_any"])
            out["large_objects_ahead_any"].append(tag["large_objects_ahead_any"])
        else:
            small = max(cols[i]["small_objects_near"] for i in idx)
            out["small_objects_near_max"].append(small)
            out["small_objects_near_any"].append(small > 0)
            out["large_objects_ahead_any"].append(any(cols[i]["large_objects_ahead"] > 0 for i in idx))
    schema = segments_schema(vectors.shape[1])
    return pa.table({f.name: pa.array(out[f.name], type=f.type) for f in schema}, schema=schema)
