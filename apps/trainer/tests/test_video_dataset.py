"""動画ローダー（VideoManifestDataset）のテスト。

生成器（別の uv プロジェクト）で動画入りの MCAP を作り、indexer で索引 → タグ付け → 選別をしてから読む。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from indexer.build_index import build_index
from indexer.select import SelectParams, select_and_curate
from indexer.storage import open_location
from indexer.tag import tag_segments

from trainer.video_dataset import VideoManifestDataset

GENERATOR = Path(__file__).resolve().parents[2] / "mcap-generator"


@pytest.fixture(scope="module")
def manifest(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("uv") is None:
        pytest.skip("uv がないので生成器を実行できません")
    root = tmp_path_factory.mktemp("video")
    raw, derived, curated = root / "raw", root / "derived", root / "curated"
    subprocess.run(
        ["uv", "run", "-q", "--project", str(GENERATOR), "mcap-generate", "--out", str(raw), "--video", "--first-device", "31",
         "--devices", "2", "--sessions-per-device", "2", "--min-duration", "20", "--max-duration", "20", "--seed", "5"],
        check=True, capture_output=True,
    )  # fmt: skip
    build_index(str(raw), str(derived))
    tag_segments(open_location(str(raw)), open_location(str(derived)), str(raw), str(derived))
    sql = root / "first_segments.sql"
    sql.write_text("SELECT segment_id FROM segments WHERE segment_id LIKE '%_000'")  # 各セッションの最初の 10 秒だけ
    select_and_curate(str(raw), str(derived), open_location(str(curated)), SelectParams("v", 1, str(sql), frames="random", seed=1, gops_per_shard=7))
    return {"raw": raw, "manifest": curated / "v" / "v1" / "manifest.json"}


def test_先読みの有無で同じサンプルが同じ順に出る(manifest: dict[str, Path]) -> None:
    a = list(VideoManifestDataset(str(manifest["manifest"]), prefetch=0))
    b = list(VideoManifestDataset(str(manifest["manifest"]), prefetch=3))
    assert len(a) == len(b) == 4 * 10  # 4 セッション × 1 セグメント × 10 GOP（random は GOP ごとに 1 フレーム）
    for x, y in zip(a, b, strict=True):
        assert (x["segment_id"], x["frame_index"]) == (y["segment_id"], y["frame_index"])
        assert x["image"].equal(y["image"]) and x["route"].equal(y["route"])
    s = a[0]
    assert tuple(s["image"].shape) == (3, 180, 320) and tuple(s["route"].shape) == (1, 128, 128)
    assert s["time_of_day"] in {"day", "dusk", "night"} and s["heading_source"] in {"gnss", "route"}
    assert int(s["route"].max()) == 255  # これから進むルートが描かれている


def test_読むのは選ばれたGOPのチャンクとsummaryだけ(manifest: dict[str, Path]) -> None:
    import json

    ds = VideoManifestDataset(str(manifest["manifest"]), prefetch=2)
    n = sum(1 for _ in ds)
    m = json.loads(manifest["manifest"].read_text())
    read, _ = ds.stats.snapshot()
    assert n == m["counts"]["samples"]
    assert m["selected_chunk_bytes"] <= read < m["selected_chunk_bytes"] * 1.2  # チャンク + summary・ルート・シャードの少し
    assert read < m["selected_mcap_bytes"] * 0.7  # 20 秒のうち最初の 10 秒だけなので、MCAP 全体の約半分


def test_分散学習のランクでシャードが分かれる(manifest: dict[str, Path]) -> None:
    ids = [
        {(s["segment_id"], s["frame_index"]) for s in VideoManifestDataset(str(manifest["manifest"]), prefetch=0, rank=r, world_size=2)}
        for r in (0, 1)
    ]
    assert ids[0] and ids[1] and not (ids[0] & ids[1])
    assert len(ids[0] | ids[1]) == 40
