"""系譜マニフェスト（_manifests）と隔離記録（_quarantine）の読み書き。

マニフェストは「このセッションの変換は完了した」という印を兼ねる。
S3 にはファイル名をまとめて入れ替える（アトミックな rename）機能が無いので、
「出力をすべて書き終えてから、最後にマニフェストを書く」ことで、途中で落ちても
マニフェストが無い = 未完了 と判断できるようにしている。
"""

from __future__ import annotations

import platform
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from converter import CONVERTER_NAME
from converter.config import Settings
from converter.layout import SessionRef, manifest_key, quarantine_key
from converter.storage import Location, read_json, write_json

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WrittenFile:
    """書き出した 1 ファイルの情報（マニフェストに記録する）。"""

    path: str  # 例: s3://derived/telemetry/imu/.../part-00000.parquet
    rows: int
    size_bytes: int
    sha256: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _converter_info(settings: Settings) -> dict[str, Any]:
    """変換器の情報。version がイメージタグと同じ値になる。"""
    import pyarrow
    import ray

    return {
        "name": CONVERTER_NAME,
        "version": settings.converter_version,
        "image": settings.converter_image,
        "ray_version": ray.__version__,
        "pyarrow_version": pyarrow.__version__,
        "python_version": platform.python_version(),
    }


def _input_info(raw: Location, ref: SessionRef) -> dict[str, Any]:
    return {
        "mcap": raw.uri_of(ref.mcap_key),
        "sha256": ref.input_sha256,
        "size_bytes": ref.input_size,
        "metadata": raw.uri_of(ref.metadata_key),
        "metadata_sha256": ref.metadata_sha256,
    }


def build_manifest(
    settings: Settings,
    raw: Location,
    ref: SessionRef,
    outputs: dict[str, list[WrittenFile]],
    topic_counts: dict[str, int],
    ignored_topics: dict[str, int],
) -> dict[str, Any]:
    """1 セッション分のマニフェスト（系譜情報）を作る。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "converted",
        "device_id": ref.device_id,
        "date": ref.date,
        "session_id": ref.session_id,
        "converted_at": _now(),
        "converter": _converter_info(settings),
        "input": _input_info(raw, ref),
        # データセットごとの出力ファイル（パス・行数・サイズ・sha256）
        "outputs": {
            name: {"rows": sum(f.rows for f in files), "files": [asdict(f) for f in files]}
            for name, files in outputs.items()
        },
        # トピックごとの行数（= 変換したメッセージ数）。metadata.json の message_count と一致するはず
        "topic_row_counts": topic_counts,
        "ignored_topics": ignored_topics,
    }


def build_quarantine(settings: Settings, raw: Location, ref: SessionRef, stage: str, error: BaseException) -> dict[str, Any]:
    """変換に失敗したセッションの記録を作る。トレースバックは末尾だけ残す（長くなりすぎないように）。"""
    tb = traceback.format_exception(error)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "quarantined",
        "device_id": ref.device_id,
        "date": ref.date,
        "session_id": ref.session_id,
        "quarantined_at": _now(),
        "stage": stage,  # preflight（事前チェック）/ read（read_mcap）/ convert（変換・書き込み）
        "error": f"{type(error).__name__}: {error}",
        "traceback_tail": "".join(tb)[-2000:],
        "converter": _converter_info(settings),
        "input": _input_info(raw, ref),
    }


def write_quarantine(settings: Settings, raw: Location, derived: Location, ref: SessionRef, stage: str, error: BaseException) -> None:
    write_json(derived, quarantine_key(ref.session_id), build_quarantine(settings, raw, ref, stage, error))


def matches(record: dict[str, Any] | None, ref: SessionRef, converter_version: str) -> bool:
    """記録（マニフェスト / 隔離記録）が「同じ入力・同じ変換器」で作られたものか。

    入力 MCAP の sha256 と変換器のバージョンが両方同じなら、変換結果も同じになるはずなので
    やり直す必要がない（冪等性の判定）。
    """
    if record is None:
        return False
    try:
        return record["input"]["sha256"] == ref.input_sha256 and record["converter"]["version"] == converter_version
    except (KeyError, TypeError):
        return False  # 形式の壊れた記録は「無い」のと同じ扱い（作り直す）


def session_state(derived: Location, ref: SessionRef, converter_version: str) -> str:
    """セッションの状態を返す: converted（変換済み）/ quarantined（隔離済み）/ pending（未処理・要再変換）。"""
    if matches(read_json(derived, manifest_key(ref.session_id)), ref, converter_version):
        return "converted"
    if matches(read_json(derived, quarantine_key(ref.session_id)), ref, converter_version):
        return "quarantined"
    return "pending"
