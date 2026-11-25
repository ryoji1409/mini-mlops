"""1 セッション分の MCAP と metadata.json を書き出すモジュール。

出力先のディレクトリ構成は、S3 の raw ゾーンと同じパーティション形式にする:
    <out>/device=<id>/date=<YYYY-MM-DD>/session_id=<id>/<session_id>.mcap
    <out>/device=<id>/date=<YYYY-MM-DD>/session_id=<id>/metadata.json
    <out>/device=<id>/date=<YYYY-MM-DD>/session_id=<id>/ground_truth.parquet   （--video のときだけ）
こうしておくと、アップロード時のキーとローカルのパスが一目で対応する。

--video のときは、次も書く（フェーズ 5: 動画の索引・選別・高速な読み出し）:
- /camera/front/compressed_video: H.265 の動画（10Hz、GOP 1 秒）。video.py
- /navigation/route: 予定ルート（記録の先頭に 1 回）。route.py
- MCAP のチャンクを GOP の先頭で区切る（1 チャンク = 1 秒 = 1 GOP）。
  索引（apps/indexer）は「チャンクの開始位置と長さ」を GOP の参照位置として記録し、
  データローダはその範囲だけを S3 の Range GET で取ってデコードする。
  チャンクは zstd で丸ごと圧縮されるので、取り出せる最小の単位はチャンク 1 つ。
  GOP の先頭でチャンクを区切っておけば、「1 回の Range GET = ちょうど 1 GOP」になり、
  前後の GOP のデータを余分に取らずに済む（同じ 1 秒の IMU / GNSS / JPEG は同じチャンクに同居する）。
  予定ルートは先頭の小さなチャンクに単独で入れる
"""

import hashlib
import io
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo
from foxglove_schemas_protobuf.LocationFix_pb2 import LocationFix
from foxglove_schemas_protobuf.LocationFixes_pb2 import LocationFixes
from google.protobuf.message import Message
from mcap.writer import CompressionType
from mcap_protobuf.writer import Writer
from PIL import Image

from . import GENERATOR_NAME, GENERATOR_VERSION
from .ground_truth import FRAMES_PER_SEGMENT, GROUND_TRUTH_FILE, SEGMENT_LENGTH_S, GroundTruthTable
from .imu_schema import Imu
from .route import ROUTE_SPACING_M, make_route
from .scenario import SessionScenario
from .scene import HEIGHT, WIDTH, frame_truth, make_layout, render_frame
from .video import GOP_LENGTH, VIDEO_HEIGHT, VIDEO_RATE_HZ, VIDEO_WIDTH, H265Encoder
from .sensors import (
    BASE_RATE_HZ,
    DT_S,
    GnssSample,
    ImuSample,
    gnss_sample,
    imu_sample,
    device_origin,
    make_trajectory,
)

# metadata.json の形式のバージョン。項目を変えたら上げる（後段の処理が形式を判別できるように）
# 2: --video のとき、video / segments / sidecars（付属ファイル）の項目が増える
METADATA_SCHEMA_VERSION = 1
METADATA_SCHEMA_VERSION_VIDEO = 2


@dataclass(frozen=True)
class TopicSpec:
    """1 トピック分の設定。"""

    topic: str
    schema: str
    rate_hz: int
    frame_id: str

    @property
    def every_n_ticks(self) -> int:
        """100Hz の刻みの何回に 1 回メッセージを出すか（例: 5Hz なら 20 回に 1 回）。"""
        return BASE_RATE_HZ // self.rate_hz


CAMERA = TopicSpec("/camera/front/image_compressed", "foxglove.CompressedImage", 5, "camera_front")
IMU = TopicSpec("/imu", "mini_mlops.Imu", 100, "imu")
GNSS = TopicSpec("/gnss", "foxglove.LocationFix", 10, "gnss")
TOPICS: tuple[TopicSpec, ...] = (CAMERA, IMU, GNSS)
VIDEO = TopicSpec("/camera/front/compressed_video", "foxglove.CompressedVideo", VIDEO_RATE_HZ, "camera_front")
# 予定ルートは記録の先頭に 1 回だけ出す（rate_hz=0 は「定期的には出さない」の意味）
ROUTE = TopicSpec("/navigation/route", "foxglove.LocationFixes", 0, "map")
VIDEO_TOPICS: tuple[TopicSpec, ...] = (CAMERA, VIDEO, IMU, GNSS, ROUTE)

# --video のときのチャンクの上限サイズ。GOP の先頭で自分で区切るので、自動では区切られないよう十分大きくする
VIDEO_CHUNK_SIZE = 64 * 1024 * 1024

JPEG_QUALITY = 85


def partition_dir(scenario: SessionScenario) -> Path:
    """raw ゾーンのパーティション（相対パス）を返す。date は開始時刻の UTC 日付。"""
    return Path(
        f"device={scenario.device_id}",
        f"date={scenario.start_time:%Y-%m-%d}",
        f"session_id={scenario.session_id}",
    )


# ---------------------------------------------------------------------------
# メッセージ（protobuf）を組み立てる関数
# ---------------------------------------------------------------------------


def _camera_message(t_ns: int, jpeg: bytes) -> CompressedImage:
    msg = CompressedImage(frame_id=CAMERA.frame_id, data=jpeg, format="jpeg")
    msg.timestamp.FromNanoseconds(t_ns)
    return msg


def _imu_message(t_ns: int, sample: ImuSample) -> Message:
    msg = Imu(frame_id=IMU.frame_id)
    msg.timestamp.FromNanoseconds(t_ns)
    ox, oy, oz, ow = sample.orientation_xyzw
    msg.orientation.x, msg.orientation.y, msg.orientation.z, msg.orientation.w = ox, oy, oz, ow
    msg.angular_velocity.x, msg.angular_velocity.y, msg.angular_velocity.z = sample.angular_velocity
    msg.linear_acceleration.x, msg.linear_acceleration.y, msg.linear_acceleration.z = sample.linear_acceleration
    return msg


def _video_message(t_ns: int, data: bytes) -> CompressedVideo:
    msg = CompressedVideo(frame_id=VIDEO.frame_id, data=data, format="h265")
    msg.timestamp.FromNanoseconds(t_ns)
    return msg


def _route_message(t_ns: int, points: list[tuple[float, float]]) -> LocationFixes:
    msg = LocationFixes()
    for lat, lon in points:
        fix = msg.fixes.add(frame_id=ROUTE.frame_id, latitude=lat, longitude=lon)
        fix.timestamp.FromNanoseconds(t_ns)
    return msg


def _gnss_message(t_ns: int, sample: GnssSample) -> LocationFix:
    var = sample.sigma_m**2
    msg = LocationFix(
        frame_id=GNSS.frame_id,
        latitude=sample.latitude,
        longitude=sample.longitude,
        altitude=sample.altitude,
        # 3x3 の共分散行列（東・北・上）。対角成分だけ分かっている、という意味の型を指定する
        position_covariance=[var, 0, 0, 0, var, 0, 0, 0, (2 * sample.sigma_m) ** 2],
        position_covariance_type=LocationFix.PositionCovarianceType.DIAGONAL_KNOWN,
    )
    msg.timestamp.FromNanoseconds(t_ns)
    return msg


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# MCAP の書き出し
# ---------------------------------------------------------------------------


def _device_index(device_id: str) -> int:
    """"dev-002" → 1 のように、デバイス ID から 0 始まりの番号を取り出す。"""
    return int(device_id.rsplit("-", 1)[1]) - 1


def _write_messages(writer: Writer, scenario: SessionScenario, video: bool = False) -> tuple[Counter[str], GroundTruthTable | None]:
    """100Hz の刻みで時刻を進めながら、各トピックのメッセージを時刻順に書き込む。

    戻り値はトピックごとのメッセージ数（metadata.json に書く）と、video のときはフレームごとの正解値。
    video=False のときは、フェーズ 1 からの出力と 1 バイトも変わらない（描画の回数・乱数の使い方を変えない）。
    """
    # 用途ごとに乱数の流れを分ける。こうすると、例えば画像の描き方を変えても
    # IMU / GNSS の値は変わらない（変更の影響範囲が分かりやすい）。
    seq = np.random.SeedSequence(scenario.seed)
    rng_traj, rng_layout, rng_imu, rng_gnss, rng_rain = (np.random.default_rng(s) for s in seq.spawn(5))

    traj = make_trajectory(
        rng_traj, scenario.duration_s, scenario.base_speed_mps, device_origin(_device_index(scenario.device_id))
    )
    layout = make_layout(scenario, rng_layout)
    travelled = np.cumsum(traj.speed_mps) * DT_S  # 移動距離 [m]（床の格子などを動かすのに使う）

    counts: Counter[str] = Counter()
    tick_ns = 1_000_000_000 // BASE_RATE_HZ  # 10ms。整数のナノ秒で扱い、小数の誤差をためない
    encoder = H265Encoder() if video else None
    truth = GroundTruthTable(scenario.session_id, scenario.time_of_day, scenario.weather) if video else None

    for i in range(len(traj)):
        t_ns = scenario.start_time_ns + i * tick_ns
        img = None
        if encoder is not None and truth is not None and i % VIDEO.every_n_ticks == 0:
            frame_index = i // VIDEO.every_n_ticks
            if i == 0:
                # 予定ルートは先頭に 1 回。直後の flush で「ルートだけのチャンク」になるので、
                # 索引作成はルートを取り出すときに GOP のデータを読まずに済む
                writer.write_message(ROUTE.topic, _route_message(t_ns, make_route(traj)), log_time=t_ns, publish_time=t_ns)
                counts[ROUTE.topic] += 1
            if frame_index % GOP_LENGTH == 0:
                # GOP の先頭（IDR）を書く直前に、作りかけのチャンクを閉じる → 1 チャンク = 1 GOP になる。
                # mcap_protobuf の Writer は中に mcap の Writer（_writer）を持っていて、その flush() が
                # 「作りかけのチャンクを閉じて書き出す」公開のメソッド（mcap 1.6.0）
                writer._writer.flush()
            img = render_frame(scenario, layout, i * DT_S, float(travelled[i]), rng_rain)
            data, is_key = encoder.encode(img)
            if is_key != (frame_index % GOP_LENGTH == 0):
                raise RuntimeError(f"フレーム {frame_index}: IDR の位置が GOP の先頭とずれています")
            writer.write_message(VIDEO.topic, _video_message(t_ns, data), log_time=t_ns, publish_time=t_ns)
            counts[VIDEO.topic] += 1
            truth.add(frame_index, t_ns, frame_truth(layout, i * DT_S, float(travelled[i])))
        if i % CAMERA.every_n_ticks == 0:
            if img is None:  # video のときは動画用に描いた画像をそのまま使う（同じ時刻なので同じ絵）
                img = render_frame(scenario, layout, i * DT_S, float(travelled[i]), rng_rain)
            writer.write_message(CAMERA.topic, _camera_message(t_ns, _encode_jpeg(img)), log_time=t_ns, publish_time=t_ns)
            counts[CAMERA.topic] += 1
        if i % IMU.every_n_ticks == 0:
            writer.write_message(IMU.topic, _imu_message(t_ns, imu_sample(traj, i, rng_imu)), log_time=t_ns, publish_time=t_ns)
            counts[IMU.topic] += 1
        if i % GNSS.every_n_ticks == 0:
            writer.write_message(GNSS.topic, _gnss_message(t_ns, gnss_sample(traj, i, rng_gnss)), log_time=t_ns, publish_time=t_ns)
            counts[GNSS.topic] += 1
    if encoder is not None:
        encoder.close()
    return counts, truth


def sha256_of(path: Path) -> str:
    """ファイルの sha256 を計算する。大きなファイルでもメモリを食わないよう 1MB ずつ読む。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _build_metadata(
    scenario: SessionScenario, counts: Counter[str], mcap_path: Path, seed: int, sidecars: list[Path] | None = None
) -> dict[str, Any]:
    """metadata.json の中身を作る。時間帯・天候などは後のフェーズで「正解ラベル」として使う。"""
    video = sidecars is not None
    topics = VIDEO_TOPICS if video else TOPICS
    meta: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION_VIDEO if video else METADATA_SCHEMA_VERSION,
        "device_id": scenario.device_id,
        "session_id": scenario.session_id,
        "start_time": _iso_from_ns(scenario.start_time_ns),
        "end_time": _iso_from_ns(scenario.end_time_ns),
        "start_time_ns": scenario.start_time_ns,
        "end_time_ns": scenario.end_time_ns,
        "duration_s": scenario.duration_s,
        "conditions": {
            "time_of_day": scenario.time_of_day,
            "weather": scenario.weather,
            "num_large_objects": scenario.num_large_objects,
            "num_small_objects": scenario.num_small_objects,
        },
        "topics": [
            {
                "topic": spec.topic,
                "schema": spec.schema,
                "message_encoding": "protobuf",
                "rate_hz": spec.rate_hz,
                "message_count": counts[spec.topic],
            }
            for spec in topics
        ],
        "camera": {"width": WIDTH, "height": HEIGHT, "format": "jpeg"},
        "file": {
            "name": mcap_path.name,
            "size_bytes": mcap_path.stat().st_size,
            "sha256": sha256_of(mcap_path),
            "compression": "zstd",
        },
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION, "seed": seed, "session_seed": scenario.seed},
    }
    if video:
        meta["video"] = {
            "topic": VIDEO.topic,
            "format": "h265",
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "rate_hz": VIDEO_RATE_HZ,
            "gop_length": GOP_LENGTH,
            "b_frames": 0,
            "chunk_per_gop": True,
        }
        meta["route"] = {"topic": ROUTE.topic, "spacing_m": ROUTE_SPACING_M}
        meta["segments"] = {"length_s": SEGMENT_LENGTH_S, "frames_per_segment": FRAMES_PER_SEGMENT, "count": -(-counts[VIDEO.topic] // FRAMES_PER_SEGMENT)}
        # 付属ファイル（MCAP と一緒にアップロードする）。アップロードと検証は sha256 で照合する
        meta["sidecars"] = [{"name": p.name, "size_bytes": p.stat().st_size, "sha256": sha256_of(p)} for p in sidecars or []]
    return meta


def _iso_from_ns(t_ns: int) -> str:
    """ナノ秒の時刻を ISO 8601（UTC, 末尾 Z, ナノ秒まで）の文字列にする。

    datetime はマイクロ秒までしか持てないので、秒までを datetime で整形し、端数は自分で付ける。
    """
    seconds, nanos = divmod(t_ns, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{nanos:09d}Z"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """一時ファイルに書いてから名前を変える。途中で止まっても中途半端なファイルが残らない。"""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_session(scenario: SessionScenario, out_root: Path, seed: int, video: bool = False) -> dict[str, Any]:
    """1 セッション分の MCAP と metadata.json（video のときは ground_truth.parquet も）を書き出し、metadata の中身を返す。"""
    session_dir = out_root / partition_dir(scenario)
    session_dir.mkdir(parents=True, exist_ok=True)
    mcap_path = session_dir / f"{scenario.session_id}.mcap"
    tmp_path = session_dir / f".{mcap_path.name}.tmp"

    # MCAP は一時ファイルに書き、最後まで書けたら本来の名前に変える
    with tmp_path.open("wb") as f:
        # チャンク（メッセージのまとまり）を zstd で圧縮する。チャンクサイズは 1MB
        # （video のときは GOP の先頭で自分で区切るので、上限は十分大きくする）
        writer = Writer(f, chunk_size=VIDEO_CHUNK_SIZE if video else 1024 * 1024, compression=CompressionType.ZSTD)
        counts, truth = _write_messages(writer, scenario, video)
        writer.finish()
    os.replace(tmp_path, mcap_path)

    sidecars = None
    if truth is not None:
        gt_path = session_dir / GROUND_TRUTH_FILE
        gt_tmp = session_dir / f".{GROUND_TRUTH_FILE}.tmp"
        truth.write(gt_tmp)
        os.replace(gt_tmp, gt_path)
        sidecars = [gt_path]
    metadata = _build_metadata(scenario, counts, mcap_path, seed, sidecars)
    _atomic_write_bytes(
        session_dir / "metadata.json",
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return metadata
