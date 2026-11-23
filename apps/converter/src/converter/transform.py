"""デコードしたメッセージを、データセットごとの Arrow テーブル（列の形）にする。

方針:
- 入れ子の構造（orientation.x など）は平らにして orientation_x のような列にする
  （DuckDB / pandas / Spark で扱いやすく、列ごとの圧縮も効く）
- device_id / session_id はパス（パーティション）にもあるが、ファイルの中にも列として持つ
  （ファイル単体を取り出しても、どのセッションのデータか分かるようにするため）
  date はファイルに入れない。パスの date= と同じ名前の列があると、型の推定が食い違って
  pyarrow / Ray が「Unable to merge: Field date has incompatible types」で読めなくなるため
  （日付は timestamp 列からも分かる）。
  session_id も同じ名前でパスにあるが、どちらも文字列なので DuckDB / Ray / pyarrow.dataset では問題ない。
  ただし pyarrow.parquet.read_table は既定でパスの値を dictionary 型と推定して衝突するので、
  partitioning=None を付けて読む（README に記載）。
- 時刻は 2 種類持つ:
    timestamp   … センサーが付けた時刻（メッセージ内の timestamp）。timestamp[ns, UTC] 型
    log_time_ns … MCAP に記録された時刻（int64 ナノ秒）。トピック間の並びの基準
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pyarrow as pa
from PIL import Image

from converter.decode import timestamp_ns
from converter.layout import SessionRef

TS_TYPE = pa.timestamp("ns", tz="UTC")


def _common_columns(ref: SessionRef, n: int, ts_ns: list[int], log_time_ns: list[int]) -> dict[str, pa.Array]:
    """どのデータセットにも付ける共通の列。"""
    return {
        "device_id": pa.array([ref.device_id] * n, pa.string()),
        "session_id": pa.array([ref.session_id] * n, pa.string()),
        "timestamp": pa.array(ts_ns, pa.int64()).cast(TS_TYPE),
        "log_time_ns": pa.array(log_time_ns, pa.int64()),
    }


def imu_table(ref: SessionRef, msgs: list[Any], log_times: list[int]) -> pa.Table:
    """mini_mlops.Imu → 1 メッセージ 1 行。"""
    cols = _common_columns(ref, len(msgs), [timestamp_ns(m.timestamp) for m in msgs], log_times)
    cols["frame_id"] = pa.array([m.frame_id for m in msgs], pa.string())
    for axis in ("x", "y", "z", "w"):
        cols[f"orientation_{axis}"] = pa.array([getattr(m.orientation, axis) for m in msgs], pa.float64())
    for axis in ("x", "y", "z"):
        cols[f"angular_velocity_{axis}"] = pa.array([getattr(m.angular_velocity, axis) for m in msgs], pa.float64())
    for axis in ("x", "y", "z"):
        cols[f"linear_acceleration_{axis}"] = pa.array([getattr(m.linear_acceleration, axis) for m in msgs], pa.float64())
    return pa.table(cols)


def gnss_table(ref: SessionRef, msgs: list[Any], log_times: list[int]) -> pa.Table:
    """foxglove.LocationFix → 1 メッセージ 1 行。"""
    cols = _common_columns(ref, len(msgs), [timestamp_ns(m.timestamp) for m in msgs], log_times)
    cols["frame_id"] = pa.array([m.frame_id for m in msgs], pa.string())
    cols["latitude"] = pa.array([m.latitude for m in msgs], pa.float64())
    cols["longitude"] = pa.array([m.longitude for m in msgs], pa.float64())
    cols["altitude"] = pa.array([m.altitude for m in msgs], pa.float64())
    cols["position_covariance"] = pa.array([list(m.position_covariance) for m in msgs], pa.list_(pa.float64()))
    # 列挙型は数値で入っている（0=UNKNOWN, 1=APPROXIMATED, 2=DIAGONAL_KNOWN, 3=KNOWN）
    cols["position_covariance_type"] = pa.array([int(m.position_covariance_type) for m in msgs], pa.int32())
    return pa.table(cols)


def _image_size(data: bytes) -> tuple[int, int]:
    """JPEG / PNG の幅と高さ。Pillow はヘッダだけ読むので、画像全体はデコードしない。"""
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def frames_table(ref: SessionRef, msgs: list[Any], log_times: list[int]) -> pa.Table:
    """foxglove.CompressedImage → 1 フレーム 1 行（JPEG のバイト列をそのまま持つ）。"""
    sizes = [_image_size(m.data) for m in msgs]
    cols = _common_columns(ref, len(msgs), [timestamp_ns(m.timestamp) for m in msgs], log_times)
    cols["frame_index"] = pa.array(range(len(msgs)), pa.int32())  # セッション内での通し番号（0 始まり）
    cols["format"] = pa.array([m.format for m in msgs], pa.string())
    cols["width"] = pa.array([w for w, _ in sizes], pa.int32())
    cols["height"] = pa.array([h for _, h in sizes], pa.int32())
    cols["jpeg"] = pa.array([bytes(m.data) for m in msgs], pa.binary())
    return pa.table(cols)


# データセット名 → テーブルを作る関数
BUILDERS: dict[str, Callable[[SessionRef, list[Any], list[int]], pa.Table]] = {
    "imu": imu_table,
    "gnss": gnss_table,
    "frames_front": frames_table,
}
