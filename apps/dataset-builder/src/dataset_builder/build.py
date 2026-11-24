"""データセットを作る本体。

出力（curated ゾーン）:
    scene-cls/v<N>/train/part-00000.parquet … part-0000{train_shards-1}.parquet
    scene-cls/v<N>/val/part-00000.parquet …
    scene-cls/v<N>/manifest.json   ← 最後に書く（= 完成の印。系譜・件数・ハッシュ）

1 行 = 1 フレーム。列: image（縮小した JPEG）、time_of_day / weather（クラス番号）、session_id、device_id、frame_index

同じバージョン番号で何度実行しても同じ結果になるよう（冪等）にしている:
- 入力（使うセッションと、その derived マニフェストの sha256）と作り方（パラメータ）から input_hash を計算する
- 既に manifest.json があり input_hash が同じなら、何もしない（スキップ）
- input_hash が違うなら、上書きせずにエラー（同じ v<N> の中身が変わると、過去の学習結果と比べられなくなるため）
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from dataset_builder import BUILDER_NAME, BUILDER_VERSION
from dataset_builder.images import resize_jpeg
from dataset_builder.sources import TIME_OF_DAY_CLASSES, WEATHER_CLASSES, SessionSource, collect_sources
from dataset_builder.split import split_sessions
from dataset_builder.storage import Location, delete_files, list_files, read_bytes, read_json, write_bytes, write_json

log = logging.getLogger(__name__)

DATASET_NAME = "scene-cls"
MANIFEST_SCHEMA_VERSION = 1


class DatasetConflictError(RuntimeError):
    """同じバージョン番号のデータセットが、違う中身で既にある。"""


@dataclass(frozen=True)
class BuildParams:
    """データセットの作り方。input_hash に含めるので、変えると別のデータセットになる。"""

    version: int
    width: int = 160
    height: int = 90
    jpeg_quality: int = 90
    val_per_combo: int = 2  # （時間帯, 天候）の組ごとに、検証用に回すセッション数
    train_shards: int = 8  # 学習用のファイル数。ワーカー（rank）ごとに別々のファイルを読むので、ワーカー数で割り切れる数にする
    val_shards: int = 2
    seed: int = 42

    def prefix(self) -> str:
        return f"{DATASET_NAME}/v{self.version}"


@dataclass(frozen=True)
class BuildResult:
    status: str  # "built"（作った）/ "skipped"（同じものが既にあった）
    manifest_uri: str
    dataset_hash: str
    manifest: dict[str, Any]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_input_hash(params: BuildParams, sources: list[SessionSource]) -> str:
    """入力と作り方から、データセットの「中身を決めるもの」のハッシュを計算する。

    入力として使うのは、セッションごとの「フレームのファイルの sha256」と「ラベル（時間帯・天候）」だけ。
    derived のマニフェストのファイル全体の sha256 は使わない。マニフェストには変換した時刻（converted_at）が入っていて、
    同じ MCAP を変換し直す（force=true）と、出力のフレームはバイト単位で同じでもマニフェストは変わるため
    （最初はマニフェスト全体の sha256 を使っていて、変換し直しただけで「中身が違う」と判定された）。
    マニフェストの sha256 は、系譜の照合用にデータセットのマニフェストへ別に記録している。
    """
    payload = {
        "builder_version": BUILDER_VERSION,
        "params": asdict(params),
        "inputs": [[s.session_id, s.time_of_day, s.weather, list(s.frame_sha256s)] for s in sources],
    }
    return _sha256(json.dumps(payload, sort_keys=True).encode())


def _read_frames(derived: Location, source: SessionSource, params: BuildParams) -> list[dict[str, Any]]:
    """1 セッション分のフレームを読み、縮小して行（dict）のリストにする。"""
    rows: list[dict[str, Any]] = []
    tod = TIME_OF_DAY_CLASSES.index(source.time_of_day)
    weather = WEATHER_CLASSES.index(source.weather)
    for uri in source.frame_files:
        table = pq.read_table(io.BytesIO(read_bytes(derived, derived.rel_of_uri(uri))), columns=["frame_index", "jpeg"])
        for frame_index, jpeg in zip(table["frame_index"].to_pylist(), table["jpeg"].to_pylist(), strict=True):
            rows.append(
                {
                    "image": resize_jpeg(jpeg, params.width, params.height, params.jpeg_quality),
                    "time_of_day": tod,
                    "weather": weather,
                    "session_id": source.session_id,
                    "device_id": source.device_id,
                    "frame_index": frame_index,
                }
            )
    if len(rows) != source.frame_count:
        raise RuntimeError(f"{source.session_id}: フレーム数がマニフェストと違います（{len(rows)} != {source.frame_count}）")
    return rows


SCHEMA = pa.schema(
    [
        ("image", pa.binary()),
        ("time_of_day", pa.int8()),
        ("weather", pa.int8()),
        ("session_id", pa.string()),
        ("device_id", pa.string()),
        ("frame_index", pa.int32()),
    ]
)


def _to_parquet_bytes(rows: list[dict[str, Any]]) -> bytes:
    """行のリストを Parquet のバイト列にする。画像は JPEG で既に圧縮済みなので、Parquet の圧縮はかけない。"""
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="none")
    return sink.getvalue().to_pybytes()


def _write_shards(curated: Location, prefix: str, rows: list[dict[str, Any]], n_shards: int) -> list[dict[str, Any]]:
    """行を n_shards 個のファイルに（ほぼ同じ行数で）分けて書く。書いたファイルの情報を返す。"""
    files = []
    for i, idx in enumerate(np.array_split(np.arange(len(rows)), n_shards)):
        data = _to_parquet_bytes([rows[j] for j in idx])
        rel = f"{prefix}/part-{i:05d}.parquet"
        write_bytes(curated, rel, data)
        files.append({"path": curated.uri_of(rel), "rows": len(idx), "size_bytes": len(data), "sha256": _sha256(data)})
    return files


def _class_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """クラスごとのフレーム数（マニフェストに記録して、偏りが無いか確認できるようにする）。"""
    tod = Counter(TIME_OF_DAY_CLASSES[r["time_of_day"]] for r in rows)
    weather = Counter(WEATHER_CLASSES[r["weather"]] for r in rows)
    return {
        "time_of_day": {c: tod.get(c, 0) for c in TIME_OF_DAY_CLASSES},
        "weather": {c: weather.get(c, 0) for c in WEATHER_CLASSES},
    }


def build_dataset(raw: Location, derived: Location, curated: Location, params: BuildParams) -> BuildResult:
    """データセットを作る（同じものが既にあればスキップ）。"""
    prefix = params.prefix()
    manifest_rel = f"{prefix}/manifest.json"
    sources, skipped = collect_sources(raw, derived)
    if not sources:
        raise RuntimeError("変換済みのセッションが 1 件もありません（先に mcap-convert を実行してください）")
    input_hash = compute_input_hash(params, sources)

    existing = read_json(curated, manifest_rel)
    if existing is not None:
        if existing["input_hash"] == input_hash:
            log.info("%s は同じ中身で既にあります（スキップ）。dataset_hash=%s", curated.uri_of(prefix), existing["dataset_hash"])
            return BuildResult("skipped", curated.uri_of(manifest_rel), existing["dataset_hash"], existing)
        raise DatasetConflictError(
            f"{curated.uri_of(prefix)} は別の中身で既にあります（input_hash が違う）。"
            "入力のセッションか作り方が変わったので、新しいバージョン番号で作ってください"
        )

    # マニフェストが無いのにファイルが残っている = 前回が途中で落ちた。残骸を消してから作り直す
    leftovers = list_files(curated, prefix)
    if leftovers:
        log.warning("前回の残骸を消します: %d ファイル", len(leftovers))
        delete_files(curated, leftovers)

    assignment = split_sessions(sources, params.val_per_combo, params.seed)
    rows: dict[str, list[dict[str, Any]]] = {"train": [], "val": []}
    for i, source in enumerate(sources, start=1):
        rows[assignment[source.session_id]].extend(_read_frames(derived, source, params))
        log.info("[%d/%d] %s %s/%s → %s", i, len(sources), source.session_id, source.time_of_day, source.weather, assignment[source.session_id])

    # 学習用は行の順番を混ぜてから分ける（どのシャードにも全クラスが入るように）。検証用は順番どおりでよい
    order = np.random.default_rng(params.seed).permutation(len(rows["train"]))
    rows["train"] = [rows["train"][i] for i in order]

    splits: dict[str, Any] = {}
    for split, n_shards in (("train", params.train_shards), ("val", params.val_shards)):
        files = _write_shards(curated, f"{prefix}/{split}", rows[split], n_shards)
        splits[split] = {
            "sessions": sorted(s.session_id for s in sources if assignment[s.session_id] == split),
            "frames": len(rows[split]),
            "class_counts": _class_counts(rows[split]),
            "files": files,
        }

    # データセットそのもののハッシュ = 全ファイルの sha256 から計算（学習側の系譜のタグに使う）
    all_files = sorted(f["path"] + ":" + f["sha256"] for s in splits.values() for f in s["files"])
    dataset_hash = _sha256("\n".join(all_files).encode())
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "name": DATASET_NAME,
        "version": params.version,
        "uri": curated.uri_of(prefix),
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "builder": {"name": BUILDER_NAME, "version": BUILDER_VERSION},
        "params": asdict(params),
        "labels": {"time_of_day": list(TIME_OF_DAY_CLASSES), "weather": list(WEATHER_CLASSES)},
        "split_method": "session（（時間帯, 天候）の組ごとに val_per_combo セッションを検証用に。同じセッションのフレームは片方にしか入らない）",
        "input_hash": input_hash,
        "dataset_hash": dataset_hash,
        "converter_versions": sorted({s.converter_version for s in sources}),
        # 系譜: どの derived マニフェストから作ったか（そこから raw の MCAP までたどれる）
        "inputs": [
            {
                "session_id": s.session_id,
                "device_id": s.device_id,
                "time_of_day": s.time_of_day,
                "weather": s.weather,
                "split": assignment[s.session_id],
                "frames": s.frame_count,
                "derived_manifest": s.derived_manifest,
                "derived_manifest_sha256": s.derived_manifest_sha256,
                "converter_version": s.converter_version,
            }
            for s in sources
        ],
        "skipped": skipped,
        "splits": splits,
    }
    # マニフェストは最後に書く（これがあれば「データセットは完成している」とみなせる）
    write_json(curated, manifest_rel, manifest)
    log.info(
        "作成しました: %s（学習 %d フレーム / %d セッション、検証 %d フレーム / %d セッション）dataset_hash=%s",
        curated.uri_of(prefix),
        splits["train"]["frames"],
        len(splits["train"]["sessions"]),
        splits["val"]["frames"],
        len(splits["val"]["sessions"]),
        dataset_hash,
    )
    return BuildResult("built", curated.uri_of(manifest_rel), dataset_hash, manifest)
