"""raw / derived のキー設計（どこに何を置くか）を 1 か所にまとめたモジュール。

derived ゾーン:
    telemetry/imu/device=<v>/date=<d>/session_id=<id>/part-00000.parquet
    telemetry/gnss/device=<v>/date=<d>/session_id=<id>/part-00000.parquet
    frames/front/device=<v>/date=<d>/session_id=<id>/part-00000.parquet（32MB を超えたら part-00001 …）
    _manifests/<session_id>.json    ← 系譜（変換が完了した印も兼ねる）
    _quarantine/<session_id>.json   ← 変換できなかったセッションの記録
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

# MCAP のトピック → 出力データセットの対応表。
# ここに無いトピックは変換せず、マニフェストの ignored_topics に件数だけ記録する。
@dataclass(frozen=True)
class DatasetSpec:
    name: str  # マニフェスト・検証で使う名前
    prefix: str  # derived の中の置き場所
    schema_name: str  # このトピックに期待する protobuf の型名


DATASETS: dict[str, DatasetSpec] = {
    "/imu": DatasetSpec("imu", "telemetry/imu", "mini_mlops.Imu"),
    "/gnss": DatasetSpec("gnss", "telemetry/gnss", "foxglove.LocationFix"),
    "/camera/front/image_compressed": DatasetSpec("frames_front", "frames/front", "foxglove.CompressedImage"),
}

MANIFEST_DIR = "_manifests"
QUARANTINE_DIR = "_quarantine"

# raw の「device=…/date=…/session_id=…/metadata.json」からパーティションの値を取り出す正規表現
_PARTITION_RE = re.compile(r"device=(?P<device>[^/]+)/date=(?P<date>[^/]+)/session_id=(?P<session>[^/]+)/metadata\.json$")


@dataclass(frozen=True)
class SessionRef:
    """raw ゾーンにある 1 セッション分の情報（metadata.json から作る）。"""

    device_id: str
    date: str  # YYYY-MM-DD（UTC）。raw のパーティションの値をそのまま使う
    session_id: str
    metadata_key: str  # raw の中の metadata.json の相対パス
    mcap_key: str  # raw の中の MCAP の相対パス
    input_sha256: str  # metadata.json に書かれた MCAP の sha256（アップロード前にホストで計算した値）
    input_size: int
    metadata_sha256: str  # metadata.json 自体の sha256（metadata が差し替わったことも検知するため）
    expected_counts: tuple[tuple[str, int], ...]  # (トピック, メッセージ数)。dict は frozen にできないのでタプル

    @property
    def partition(self) -> str:
        """derived でも raw と同じパーティション（Hive 形式）を使う。"""
        return f"device={self.device_id}/date={self.date}/session_id={self.session_id}"

    def expected(self) -> dict[str, int]:
        return dict(self.expected_counts)


def parse_partition(metadata_key: str) -> tuple[str, str, str] | None:
    """metadata.json のパスから (device, date, session_id) を取り出す。形式が違えば None。"""
    m = _PARTITION_RE.search(metadata_key)
    return (m["device"], m["date"], m["session"]) if m else None


def session_ref_from_metadata(metadata_key: str, raw_bytes: bytes) -> SessionRef:
    """metadata.json の中身から SessionRef を作る。必要な項目が無ければ例外（KeyError など）。"""
    parts = parse_partition(metadata_key)
    if parts is None:
        raise ValueError(f"パーティション形式ではないパスです: {metadata_key}")
    device, date, session = parts
    meta = json.loads(raw_bytes)
    if meta["session_id"] != session or meta["device_id"] != device:
        raise ValueError(f"metadata.json の session_id / device_id がパスと一致しません: {metadata_key}")
    base_dir = metadata_key.rsplit("/", 1)[0]
    return SessionRef(
        device_id=device,
        date=date,
        session_id=session,
        metadata_key=metadata_key,
        mcap_key=f"{base_dir}/{meta['file']['name']}",
        input_sha256=meta["file"]["sha256"],
        input_size=int(meta["file"]["size_bytes"]),
        metadata_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        expected_counts=tuple((t["topic"], int(t["message_count"])) for t in meta["topics"]),
    )


def output_dir(spec: DatasetSpec, ref: SessionRef) -> str:
    """あるセッション・あるデータセットの出力ディレクトリ（derived の中の相対パス）。"""
    return f"{spec.prefix}/{ref.partition}"


def part_name(index: int) -> str:
    """出力ファイル名。UUID ではなく連番にして、再実行しても同じ名前になるようにする。"""
    return f"part-{index:05d}.parquet"


def manifest_key(session_id: str) -> str:
    return f"{MANIFEST_DIR}/{session_id}.json"


def quarantine_key(session_id: str) -> str:
    return f"{QUARANTINE_DIR}/{session_id}.json"
