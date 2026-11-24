"""学習データ（curated の scene-cls データセット）の読み込み。

データセットは dataset-builder が作る:
    <dataset_uri>/manifest.json, <dataset_uri>/train/part-*.parquet, <dataset_uri>/val/part-*.parquet
1 行 = 1 フレーム。列: image（160x90 の JPEG）、time_of_day（0〜2）、weather（0〜1）ほか

分散学習では、各ワーカー（rank）が **別々のファイル（シャード）** を読む（torchrun 版）。
Ray Train 版は Ray Data がワーカーに行を配るので、ここの assign_shards は使わない（ray_train.py）。
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from PIL import Image

from trainer.storage import read_bytes, split_uri


@dataclass
class Frames:
    """メモリ上のフレーム。画像は JPEG のまま持ち、バッチを作るときにデコードする（メモリの節約）。"""

    images: list[bytes]
    time_of_day: np.ndarray  # int64、クラス番号
    weather: np.ndarray

    def __len__(self) -> int:
        return len(self.images)


def load_manifest(dataset_uri: str) -> dict[str, Any]:
    """データセットのマニフェスト（件数・ラベル・ハッシュ・系譜）を読む。"""
    loc, prefix = split_uri(dataset_uri.rstrip("/"))
    return json.loads(read_bytes(loc, f"{prefix}/manifest.json" if prefix else "manifest.json"))


def shard_uris(manifest: dict[str, Any], split: str) -> list[str]:
    """学習用（train）/ 検証用（val）のファイルの URI。"""
    return [f["path"] for f in manifest["splits"][split]["files"]]


def assign_shards(uris: list[str], rank: int, world_size: int) -> list[str]:
    """rank ごとに担当するファイルを決める（ラウンドロビン: rank 0 は 0, 2, 4…番目、rank 1 は 1, 3, 5…番目）。

    ファイル数がワーカー数で割り切れないと担当の行数がずれるが、バッチ数は全 rank で最小に揃える（torchrun_train.py）。
    """
    if world_size > len(uris):
        raise ValueError(f"ワーカー数（{world_size}）がファイル数（{len(uris)}）より多いので、ファイルを分担できません")
    return uris[rank::world_size]


def table_to_frames(table: pa.Table) -> Frames:
    return Frames(
        images=table["image"].to_pylist(),
        time_of_day=table["time_of_day"].to_numpy().astype(np.int64),
        weather=table["weather"].to_numpy().astype(np.int64),
    )


def read_frames(uris: list[str]) -> Frames:
    """Parquet のファイルを読んで、1 つの Frames にまとめる。"""
    tables = []
    for uri in uris:
        loc, rel = split_uri(uri)
        tables.append(pq.read_table(io.BytesIO(read_bytes(loc, rel)), columns=["image", "time_of_day", "weather"]))
    return table_to_frames(pa.concat_tables(tables))


def decode_images(jpegs: list[bytes]) -> torch.Tensor:
    """JPEG のリストを、学習に使うテンソル（N, 3, H, W、値は 0〜1）にする。"""
    arrays = []
    for data in jpegs:
        with Image.open(io.BytesIO(data)) as img:
            arrays.append(np.asarray(img.convert("RGB"), dtype=np.uint8))
    batch = torch.from_numpy(np.stack(arrays))  # (N, H, W, 3)
    return batch.permute(0, 3, 1, 2).float().div_(255.0)


def iter_batches(frames: Frames, batch_size: int, *, shuffle: bool, seed: int, epoch: int, max_batches: int | None = None) -> Iterator[tuple[torch.Tensor, torch.Tensor, torch.Tensor]]:
    """（画像, 時間帯ラベル, 天候ラベル）のバッチを順に返す。

    - shuffle=True のときは、エポックごとに順番を変える（seed とエポック番号から決めるので、再開しても同じ順番になる）
    - max_batches を渡すと、そこで打ち切る（DDP では全 rank のバッチ数を揃えないと、待ち合わせで止まってしまうため）
    """
    order = np.arange(len(frames))
    if shuffle:
        order = np.random.default_rng([seed, epoch]).permutation(len(frames))
    n_batches = len(frames) // batch_size if shuffle else -(-len(frames) // batch_size)  # 学習は端数を捨て、検証は全部使う
    if max_batches is not None:
        n_batches = min(n_batches, max_batches)
    for b in range(n_batches):
        idx = order[b * batch_size : (b + 1) * batch_size]
        yield (
            decode_images([frames.images[i] for i in idx]),
            torch.from_numpy(frames.time_of_day[idx]),
            torch.from_numpy(frames.weather[idx]),
        )


def num_train_batches(frames: Frames, batch_size: int) -> int:
    return len(frames) // batch_size
