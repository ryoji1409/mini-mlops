"""データセットの材料（どのセッションの、どのフレームを使うか）を集める。

起点は derived の _manifests/<session_id>.json（変換が完了した印）。
マニフェストには、元の raw の metadata.json のパスと、出力したフレームのファイルが書いてあるので、
そこから「ラベル（時間帯・天候）」と「画像」を結び付ける。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from dataset_builder.storage import Location, list_files, read_bytes

log = logging.getLogger(__name__)

# ラベルの語彙（合成データの生成器 apps/mcap-generator と同じ）。並び順 = クラス番号
TIME_OF_DAY_CLASSES: tuple[str, ...] = ("day", "dusk", "night")
WEATHER_CLASSES: tuple[str, ...] = ("clear", "rain")


@dataclass(frozen=True)
class SessionSource:
    """データセットに入れる 1 セッション分の材料。"""

    session_id: str
    device_id: str
    time_of_day: str
    weather: str
    derived_manifest: str  # 例: s3://derived/_manifests/<session_id>.json（系譜をたどる起点）
    derived_manifest_sha256: str  # マニフェストの中身の sha256（変換し直されたら変わる）
    converter_version: str
    frame_files: tuple[str, ...]  # derived のフレームの Parquet（URI）
    frame_sha256s: tuple[str, ...]  # 上のファイルそれぞれの sha256（変換器がマニフェストに記録したもの）
    frame_count: int

    @property
    def combo(self) -> tuple[str, str]:
        """（時間帯, 天候）の組。検証用のセッションは、この組ごとに選ぶ。"""
        return (self.time_of_day, self.weather)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_sources(raw: Location, derived: Location) -> tuple[list[SessionSource], list[str]]:
    """変換済みのセッションを集める。戻り値は（使うセッション, 使えなかった理由のリスト）。

    隔離されたセッション（_quarantine）はマニフェストが無いので、自然に対象外になる。
    """
    sources: list[SessionSource] = []
    skipped: list[str] = []
    for rel in list_files(derived, "_manifests", suffix=".json"):
        data = read_bytes(derived, rel)
        manifest = json.loads(data)
        session_id = manifest.get("session_id", rel)
        if manifest.get("status") != "converted":
            skipped.append(f"{session_id}: マニフェストの status が converted ではありません")
            continue
        frames = manifest.get("outputs", {}).get("frames_front")
        if not frames or not frames.get("files"):
            skipped.append(f"{session_id}: フレームの出力がありません")
            continue
        # ラベルは raw の metadata.json の conditions から取る（変換器はラベルを持たないため）
        meta = json.loads(read_bytes(raw, raw.rel_of_uri(manifest["input"]["metadata"])))
        conditions = meta["conditions"]
        tod, weather = conditions["time_of_day"], conditions["weather"]
        if tod not in TIME_OF_DAY_CLASSES or weather not in WEATHER_CLASSES:
            skipped.append(f"{session_id}: 知らないラベルです（{tod} / {weather}）")
            continue
        sources.append(
            SessionSource(
                session_id=session_id,
                device_id=manifest["device_id"],
                time_of_day=tod,
                weather=weather,
                derived_manifest=derived.uri_of(rel),
                derived_manifest_sha256=_sha256(data),
                converter_version=manifest["converter"]["version"],
                frame_files=tuple(f["path"] for f in frames["files"]),
                frame_sha256s=tuple(f["sha256"] for f in frames["files"]),
                frame_count=int(frames["rows"]),
            )
        )
    for reason in skipped:
        log.warning("対象外: %s", reason)
    return sorted(sources, key=lambda s: s.session_id), skipped
