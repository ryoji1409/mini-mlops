"""フレームごとの正解値（ground_truth.parquet）を作るモジュール（--video のときだけ使う）。

実際の運用では、正解値はラベル付けの工程で後から付く（MCAP の中ではなく別のデータ）。
それに合わせて、MCAP とは別のファイル（セッションのディレクトリに置く「付属ファイル」）にする。

1 行 = 動画の 1 フレーム。列:
    session_id, segment_id, frame_index（セッション内の動画フレーム番号）, gop_index, frame_in_gop,
    log_time_ns, time_of_day, weather, small_objects_near, large_objects_ahead, large_objects_visible

セグメント: セッションを 10 秒ごとに区切った単位。segment_id = <session_id>_<連番 3 桁>（例: dev-031-20261102T010203Z_000）
セグメントごとの正解値（最大値・平均値・1 回でも写ったか）は、後段（apps/indexer の tag）でこの表から集計する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .scene import FrameTruth
from .video import GOP_LENGTH, VIDEO_RATE_HZ

SEGMENT_LENGTH_S = 10
FRAMES_PER_SEGMENT = SEGMENT_LENGTH_S * VIDEO_RATE_HZ
GROUND_TRUTH_FILE = "ground_truth.parquet"


def segment_id(session_id: str, frame_index: int) -> str:
    return f"{session_id}_{frame_index // FRAMES_PER_SEGMENT:03d}"


@dataclass
class GroundTruthTable:
    """フレームごとの正解値をためておき、最後に Parquet にする。"""

    session_id: str
    time_of_day: str
    weather: str
    rows: list[dict] = field(default_factory=list)

    def add(self, frame_index: int, log_time_ns: int, truth: FrameTruth) -> None:
        self.rows.append(
            {
                "session_id": self.session_id,
                "segment_id": segment_id(self.session_id, frame_index),
                "frame_index": frame_index,
                "gop_index": frame_index // GOP_LENGTH,
                "frame_in_gop": frame_index % GOP_LENGTH,
                "log_time_ns": log_time_ns,
                "time_of_day": self.time_of_day,
                "weather": self.weather,
                "small_objects_near": truth.small_objects_near,
                "large_objects_ahead": truth.large_objects_ahead,
                "large_objects_visible": truth.large_objects_visible,
            }
        )

    def write(self, path: Path) -> None:
        schema = pa.schema(
            [
                ("session_id", pa.string()),
                ("segment_id", pa.string()),
                ("frame_index", pa.int32()),
                ("gop_index", pa.int32()),
                ("frame_in_gop", pa.int8()),
                ("log_time_ns", pa.int64()),
                ("time_of_day", pa.string()),
                ("weather", pa.string()),
                ("small_objects_near", pa.int16()),
                ("large_objects_ahead", pa.int16()),
                ("large_objects_visible", pa.int16()),
            ]
        )
        table = pa.Table.from_pylist(self.rows, schema=schema)
        # 同じ入力なら同じバイト列になるよう、作成ツールの情報など揺れる要素を入れない設定で書く
        pq.write_table(table, path, compression="zstd", write_statistics=True, store_schema=True)
