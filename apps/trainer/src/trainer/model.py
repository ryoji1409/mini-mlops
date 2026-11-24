"""モデル: 小さな CNN + 出力ヘッド 2 つ（時間帯 3 クラス、天候 2 クラス）。

事前学習済みの重みは使わず、ゼロから学習する（重みの出所・ライセンスを気にしなくてよい。合成画像なので小さなモデルで足りる）。
入力は 160x90 の RGB 画像。畳み込みで特徴を取り出し、全体の平均（global average pooling）を取ってから、
2 つの全結合層（ヘッド）でそれぞれのクラスを予測する。パラメータは約 7 万。
"""

from __future__ import annotations

import torch
from torch import nn


def _block(c_in: int, c_out: int) -> nn.Sequential:
    """畳み込み → BatchNorm → ReLU → 2x2 の MaxPool（縦横を半分にする）。"""
    return nn.Sequential(
        nn.Conv2d(c_in, c_out, kernel_size=3, padding=1, bias=False),
        nn.BatchNorm2d(c_out),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),
    )


class SceneNet(nn.Module):
    def __init__(self, num_time_of_day: int = 3, num_weather: int = 2) -> None:
        super().__init__()
        # 90x160 → 45x80 → 22x40 → 11x20 → 5x10
        self.features = nn.Sequential(_block(3, 16), _block(16, 32), _block(32, 64), _block(64, 64))
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head_time_of_day = nn.Linear(64, num_time_of_day)
        self.head_weather = nn.Linear(64, num_weather)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """戻り値は（時間帯のロジット, 天候のロジット）。ロジット = softmax をかける前のスコア。"""
        h = self.pool(self.features(x)).flatten(1)
        return self.head_time_of_day(h), self.head_weather(h)
