"""テストの共通準備: 小さな学習用データセット（dataset-builder と同じ形）をローカルのディレクトリに作る。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

# クラスごとに色を変えた画像にして、小さなモデルでも学習できるようにする
TOD_COLORS = [(230, 200, 120), (200, 90, 60), (20, 20, 60)]


def _jpeg(tod: int, weather: int, rng: np.random.Generator) -> bytes:
    base = np.array(TOD_COLORS[tod], dtype=np.int16)
    img = np.clip(base + rng.integers(-20, 20, size=(90, 160, 3)), 0, 255).astype(np.uint8)
    if weather == 1:  # 雨: 縦の線を入れる
        img[:, ::6] = 180
    out = io.BytesIO()
    Image.fromarray(img).save(out, format="JPEG")
    return out.getvalue()


def _write_split(root: Path, split: str, n_rows: int, n_shards: int, rng: np.random.Generator) -> list[dict]:
    files = []
    for i, idx in enumerate(np.array_split(np.arange(n_rows), n_shards)):
        tod = rng.integers(0, 3, size=len(idx))
        weather = rng.integers(0, 2, size=len(idx))
        table = pa.table(
            {
                "image": [_jpeg(int(t), int(w), rng) for t, w in zip(tod, weather)],
                "time_of_day": pa.array(tod, pa.int8()),
                "weather": pa.array(weather, pa.int8()),
            }
        )
        path = root / split / f"part-{i:05d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path)
        files.append({"path": str(path), "rows": len(idx)})
    return files


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("curated") / "scene-cls" / "v1"
    rng = np.random.default_rng(0)
    manifest = {
        "name": "scene-cls",
        "version": 1,
        "uri": str(root),
        "dataset_hash": "testhash",
        "converter_versions": ["0.2.1"],
        "labels": {"time_of_day": ["day", "dusk", "night"], "weather": ["clear", "rain"]},
        "splits": {
            "train": {"files": _write_split(root, "train", 256, 4, rng)},
            "val": {"files": _write_split(root, "val", 64, 2, rng)},
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root
