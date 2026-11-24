"""1 エポック分の学習と、検証データでの評価。torchrun 版と Ray Train 版で共通。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import torch
from torch import nn

Batch = tuple[torch.Tensor, torch.Tensor, torch.Tensor]  # （画像, 時間帯ラベル, 天候ラベル）


def loss_fn(logits: tuple[torch.Tensor, torch.Tensor], y_tod: torch.Tensor, y_weather: torch.Tensor) -> torch.Tensor:
    """2 つのヘッドの交差エントロピーを足したものを損失にする（どちらも同じ重み）。"""
    ce = nn.functional.cross_entropy
    return ce(logits[0], y_tod) + ce(logits[1], y_weather)


def train_epoch(model: nn.Module, optimizer: torch.optim.Optimizer, batches: Iterable[Batch]) -> tuple[float, int]:
    """1 エポック学習する。戻り値は（損失の合計, サンプル数）。平均は全 rank で集計してから計算する。"""
    model.train()
    loss_sum, n = 0.0, 0
    for x, y_tod, y_weather in batches:
        optimizer.zero_grad(set_to_none=True)
        loss = loss_fn(model(x), y_tod, y_weather)
        # DDP では backward の中で、全 rank の勾配の平均を取る通信（all-reduce、gloo）が行われる
        loss.backward()
        optimizer.step()
        loss_sum += loss.item() * len(x)
        n += len(x)
    return loss_sum, n


@dataclass
class EvalSums:
    """評価の途中集計（合計値）。rank ごとに計算したものを足し合わせてから、正解率などを出す。"""

    loss_sum: float = 0.0
    n: int = 0
    # 混同行列: [正解のクラス, 予測したクラス] の件数
    confusion_tod: torch.Tensor = field(default_factory=lambda: torch.zeros(3, 3, dtype=torch.int64))
    confusion_weather: torch.Tensor = field(default_factory=lambda: torch.zeros(2, 2, dtype=torch.int64))

    def to_tensor(self) -> torch.Tensor:
        """全 rank で足し合わせる（all_reduce）ために、1 本のテンソルにまとめる。"""
        return torch.cat([torch.tensor([self.loss_sum, float(self.n)], dtype=torch.float64), self.confusion_tod.flatten().double(), self.confusion_weather.flatten().double()])

    @classmethod
    def from_tensor(cls, t: torch.Tensor) -> EvalSums:
        return cls(
            loss_sum=float(t[0]),
            n=int(t[1]),
            confusion_tod=t[2:11].round().long().reshape(3, 3),
            confusion_weather=t[11:15].round().long().reshape(2, 2),
        )

    def metrics(self) -> dict[str, float]:
        """正解率 = 混同行列の対角（正解した件数）の合計 / 全件数。"""
        n = max(self.n, 1)
        return {
            "val_loss": self.loss_sum / n,
            "val_time_of_day_accuracy": float(self.confusion_tod.diagonal().sum()) / n,
            "val_weather_accuracy": float(self.confusion_weather.diagonal().sum()) / n,
        }


@torch.no_grad()
def evaluate(model: nn.Module, batches: Iterable[Batch]) -> EvalSums:
    """検証データで評価する（この rank が持っている分だけ）。"""
    model.eval()
    sums = EvalSums()
    for x, y_tod, y_weather in batches:
        logits = model(x)
        sums.loss_sum += loss_fn(logits, y_tod, y_weather).item() * len(x)
        sums.n += len(x)
        for confusion, y, logit in ((sums.confusion_tod, y_tod, logits[0]), (sums.confusion_weather, y_weather, logits[1])):
            pred = logit.argmax(dim=1)
            # (正解, 予測) の組ごとに 1 を足す
            confusion.index_put_((y, pred), torch.ones_like(y), accumulate=True)
    return sums
