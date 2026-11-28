"""テスト用のデータとモデル。

データ: 生成器（apps/mcap-generator）で動画入り 4 セッション（各 20 秒 = 2 セグメント）を作り、indexer で索引とタグを作る。
モデル: 本物の SigLIP（813MB）は重いので、同じ形の小さな乱数のモデルを作る（中身の精度は見ない。仕組みだけ確かめる）。
       トークナイザと前処理の設定だけは、本物と同じリビジョンのファイル（数 MB）を Hugging Face から取る。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from embedder.cli import DEFAULT_HF_REPO, DEFAULT_HF_REVISION
from embedder.registry import write_manifest

GENERATOR = Path(__file__).resolve().parents[2] / "mcap-generator"
TOKENIZER_FILES = ["preprocessor_config.json", "special_tokens_map.json", "spiece.model", "tokenizer_config.json", "tokenizer.json"]


@pytest.fixture(scope="session")
def zones(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("uv") is None:
        pytest.skip("uv がないので生成器を実行できません")
    from indexer.build_index import build_index
    from indexer.storage import open_location
    from indexer.tag import tag_segments

    root = tmp_path_factory.mktemp("zones")
    raw, derived = root / "raw", root / "derived"
    subprocess.run(
        ["uv", "run", "-q", "--project", str(GENERATOR), "mcap-generate", "--out", str(raw), "--video",
         "--first-device", "31", "--devices", "2", "--sessions-per-device", "2", "--min-duration", "20", "--max-duration", "20", "--seed", "5"],
        check=True, capture_output=True,
    )
    build_index(str(raw), str(derived))
    tag_segments(open_location(str(raw)), open_location(str(derived)), str(raw), str(derived))
    return {"raw": raw, "derived": derived, "vectors": root / "vectors", "curated": root / "curated"}


@pytest.fixture(scope="session")
def tiny_model(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """本物と同じ形（SigLIP）で、層を小さくした乱数のモデル。"""
    import torch
    from huggingface_hub import hf_hub_download
    from transformers import SiglipConfig, SiglipModel

    out = tmp_path_factory.mktemp("model") / "tiny-siglip"
    out.mkdir()
    try:
        for name in TOKENIZER_FILES:
            shutil.copy(hf_hub_download(DEFAULT_HF_REPO, name, revision=DEFAULT_HF_REVISION), out / name)
    except Exception as e:  # noqa: BLE001（ネットワークが無い環境ではテストを飛ばす）
        pytest.skip(f"トークナイザのファイルを取れません: {e}")
    torch.manual_seed(0)
    small = {"hidden_size": 32, "intermediate_size": 64, "num_hidden_layers": 2, "num_attention_heads": 2}
    config = SiglipConfig(
        text_config={**small, "vocab_size": 32000, "max_position_embeddings": 64},
        vision_config={**small, "image_size": 224, "patch_size": 16},
    )
    SiglipModel(config).save_pretrained(out)
    write_manifest(out, DEFAULT_HF_REPO, "0" * 40)  # リビジョンは「本物ではない」ことが分かる値にする
    return out
