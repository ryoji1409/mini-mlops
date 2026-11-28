"""埋め込みの生成・検索・選別への受け渡し・モデルの登録のテスト（小さな乱数のモデルで仕組みだけ確かめる）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import torch

from embedder.embed import embed
from embedder.model import SiglipEmbedder
from embedder.registry import local_model, register_model, resolve_model
from embedder.search import Query, Searcher, SearchParams, save_previews, write_result
from embedder.tables import FRAMES, SEGMENTS, connect, table_name


@pytest.fixture(scope="module")
def embedder(tiny_model: Path) -> SiglipEmbedder:
    return SiglipEmbedder(local_model(tiny_model), threads=1)


def test_確率はモデル本体の出力と同じ(embedder: SiglipEmbedder) -> None:
    """match_probability（コサイン類似度から計算）が、モデル本体の logits（sigmoid の前）と一致すること。"""
    import numpy as np

    image = np.random.default_rng(0).integers(0, 255, (180, 320, 3), dtype=np.uint8)
    text = "This is a photo of night."
    cos = float(embedder.embed_images([image])[0] @ embedder.embed_texts([text])[0])
    inputs = embedder.processor(text=[text], images=[image], padding="max_length", max_length=64, return_tensors="pt")
    with torch.inference_mode():
        logit = float(embedder.model(**inputs).logits_per_image[0, 0])
    assert math.isclose(embedder.match_probability(cos), 1 / (1 + math.exp(-logit)), rel_tol=1e-4)


def test_埋め込みは冪等で間引きを変えると作り直す(zones: dict[str, Path], embedder: SiglipEmbedder) -> None:
    raw, derived, vectors = (str(zones[k]) for k in ("raw", "derived", "vectors"))
    first = embed(raw, derived, vectors, embedder, gop_stride=1, prefetch=2)
    # 4 セッション × 20 GOP（20 秒）。読む量は MCAP の合計より少ない（summary と GOP のチャンクだけ）
    assert len(first.embedded_sessions) == 4 and first.keyframes == 80 and first.segments == 8
    assert 0 < first.bytes_read < first.mcap_bytes

    again = embed(raw, derived, vectors, embedder, gop_stride=1)
    assert len(again.skipped_sessions) == 4 and again.bytes_read == 0

    db = connect(vectors)
    frames = db.open_table(table_name(embedder.ref, FRAMES))
    row = frames.to_arrow().slice(0, 1).to_pylist()[0]
    assert row["model_revision"] == embedder.ref.hf_revision and len(row["vector"]) == embedder.dim

    # 2 個おきにすると、どのセッションも GOP の組が変わるので、古い行を消して作り直す
    strided = embed(raw, derived, vectors, embedder, gop_stride=2)
    # 開いたままの表は古い版を見ているので、開き直す
    assert len(strided.embedded_sessions) == 4 and db.open_table(table_name(embedder.ref, FRAMES)).count_rows() == 40
    segments = db.open_table(table_name(embedder.ref, SEGMENTS)).to_arrow()
    assert segments.num_rows == 8 and set(segments.column("keyframes").to_pylist()) == {5}
    # 戻しておく（後のテストは全部の GOP を使う）
    embed(raw, derived, vectors, embedder, gop_stride=1)


def test_検索結果をそのまま選別に渡せる(zones: dict[str, Path], embedder: SiglipEmbedder, tmp_path: Path) -> None:
    from indexer.select import SelectParams, select_and_curate
    from indexer.storage import open_location

    raw, derived, vectors, curated = (str(zones[k]) for k in ("raw", "derived", "vectors", "curated"))
    embed(raw, derived, vectors, embedder)
    searcher = Searcher(vectors, embedder)
    query, params = Query(text="night"), SearchParams(top_k=3)
    results = searcher.search(query, params)
    assert [r["rank"] for r in results] == [1, 2, 3]
    assert all(0.0 <= r["score"] <= 1.0 for r in results)  # テキストのスコアは確率

    # 条件で絞ると、条件に合うセグメントだけが返る
    rain = searcher.search(query, SearchParams(top_k=10, where="weather = 'rain'"))
    assert rain and all(r["weather"] == "rain" for r in rain)
    # キーフレーム単位でも、セグメントは重ならない
    by_frame = searcher.search(query, SearchParams(top_k=8, level="frames"))
    assert len({r["segment_id"] for r in by_frame}) == len(by_frame) == 8

    out = tmp_path / "queries" / "night.parquet"
    write_result(results, str(out), query, params, embedder)
    assert pq.ParquetFile(out).read().column("segment_id").to_pylist() == [r["segment_id"] for r in results]
    assert json.loads(out.with_suffix(".json").read_text())["query"]["text"] == "night"

    m = select_and_curate(raw, derived, open_location(curated), SelectParams("search-night", 1, "candidates", segment_ids_uri=str(out)))
    assert m["counts"]["segments"] == 3 and m["counts"]["gops"] == 30

    previews = save_previews(searcher, results, searcher.embedder.embed_texts(["night"])[0], tmp_path / "png", 2)
    assert [p.name for p in previews] == ["rank-01.png", "rank-02.png"] and previews[0].read_bytes().startswith(b"\x89PNG")


def test_モデルをMLflowに登録してエイリアスで取り出せる(tiny_model: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # テストでは MLflow サーバーの代わりにローカルのファイルの置き場を使う
    monkeypatch.setenv("MLFLOW_TRACKING_URI", (tmp_path / "mlruns").as_uri())
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    revision = local_model(tiny_model).hf_revision
    first = register_model("example/tiny", revision, "tiny-siglip", "embedding", model_dir=tiny_model)
    again = register_model("example/tiny", revision, "tiny-siglip", "embedding", model_dir=tiny_model)
    assert first["version"] == again["version"]  # 同じリビジョンなら版を増やさない

    cache = tmp_path / "cache"
    with pytest.raises(RuntimeError, match="register-model"):
        resolve_model("tiny-siglip", "no-such-alias", cache)
    ref = resolve_model("tiny-siglip", "embedding", cache)  # 登録のときに cache_dir を渡していないので、MLflow からダウンロードする
    assert ref.version == first["version"] and ref.hf_revision == revision and (ref.path / "model.safetensors").exists()
    # キャッシュのファイルが壊れていたら、ダウンロードし直す
    (ref.path / "config.json").write_text("{}")
    assert json.loads((resolve_model("tiny-siglip", "embedding", cache).path / "config.json").read_text()) != {}

    # 新しいリビジョンを登録すると版が増え、エイリアスが新しい版に移る。cache_dir を渡すと、登録した重みがキャッシュにも置かれる
    import shutil

    from embedder.registry import write_manifest

    newer_dir = tmp_path / "newer"
    shutil.copytree(tiny_model, newer_dir)
    write_manifest(newer_dir, "example/tiny", "1" * 40)
    with pytest.raises(ValueError):  # 記録と違うリビジョンとしては登録できない
        register_model("example/tiny", "2" * 40, "tiny-siglip", "embedding", model_dir=newer_dir)
    newer = register_model("example/tiny", "1" * 40, "tiny-siglip", "embedding", model_dir=newer_dir, cache_dir=cache)
    assert int(newer["version"]) == int(first["version"]) + 1 and (cache / "tiny-siglip" / f"v{newer['version']}" / "model.safetensors").exists()
