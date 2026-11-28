"""SigLIP（Google の画像・テキストの埋め込みモデル）で、画像とテキストをベクトルにする。

埋め込み（embedding）: 画像や文章を、意味が近いものほど近くなるような数値の並び（ベクトル。SigLIP base は 768 個）に変換したもの。
SigLIP は画像とテキストを同じ空間に埋め込むので、「night」という文のベクトルと、夜の画像のベクトルが近くなる。
これを使うと、学習していない分類（ゼロショット）や、テキストでの画像検索ができる。

- ベクトルは長さ 1 にそろえる（正規化）。こうすると、内積 = コサイン類似度（-1〜1。1 に近いほど似ている）になる
- テキストと画像の「合っている確率」は、SigLIP の学習のしかたに合わせて sigmoid(scale × コサイン類似度 + bias) で求める。
  scale と bias はモデルが学習した値（重みに入っている）。しきい値（--min-score）を 0〜1 の確率で決められる
- テキストは学習時と同じく、長さ 64 の固定長にそろえる（padding="max_length"。SigLIP はこれ以外の長さだと精度が落ちる）
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

import numpy as np
import torch
from transformers import AutoModel, AutoProcessor

from embedder.registry import ModelRef

log = logging.getLogger(__name__)

# テキストで検索するときの文の型（SigLIP の論文・評価で使われている型）。「night」→「This is a photo of night.」
DEFAULT_PROMPT = "This is a photo of {}."
TEXT_MAX_LENGTH = 64


def _features(out: object) -> torch.Tensor:
    """transformers 5 では get_*_features が出力の入れ物を返す。中の埋め込み（pooler_output）を取り出す。"""
    return out.pooler_output if hasattr(out, "pooler_output") else out  # type: ignore[union-attr]


class SiglipEmbedder:
    def __init__(self, ref: ModelRef, threads: int | None = None) -> None:
        if threads:
            torch.set_num_threads(threads)  # Pod の CPU の上限に合わせる（多すぎるスレッドはかえって遅くなる）
        self.ref = ref
        # local_files_only: ここではインターネットから取らない（重みは registry.py でダウンロード済み）
        self.model = AutoModel.from_pretrained(ref.path, local_files_only=True).eval()
        self.processor = AutoProcessor.from_pretrained(ref.path, local_files_only=True)
        self.logit_scale = float(self.model.logit_scale.detach().exp())
        self.logit_bias = float(self.model.logit_bias.detach())
        self.dim = int(self.model.config.vision_config.hidden_size)

    @torch.inference_mode()
    def embed_images(self, images: Sequence[np.ndarray]) -> np.ndarray:
        """RGB 画像（H×W×3、uint8）の並び → 正規化した埋め込み（枚数×dim、float32）。画像は 224×224 に縮めてから入れる。"""
        inputs = self.processor(images=list(images), return_tensors="pt")
        feats = _features(self.model.get_image_features(**inputs))
        return torch.nn.functional.normalize(feats, dim=-1).numpy().astype(np.float32)

    @torch.inference_mode()
    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        inputs = self.processor(text=list(texts), padding="max_length", max_length=TEXT_MAX_LENGTH, truncation=True, return_tensors="pt")
        feats = _features(self.model.get_text_features(**inputs))
        return torch.nn.functional.normalize(feats, dim=-1).numpy().astype(np.float32)

    def match_probability(self, cosine: float) -> float:
        """テキストと画像のコサイン類似度 → 「合っている確率」（SigLIP の sigmoid）。"""
        return 1.0 / (1.0 + math.exp(-(self.logit_scale * cosine + self.logit_bias)))
