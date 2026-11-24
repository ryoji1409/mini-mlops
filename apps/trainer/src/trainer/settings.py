"""学習の設定（コマンドの引数と環境変数から作る）。torchrun 版と Ray Train 版で共通。"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

import torch


@dataclass(frozen=True)
class TrainSettings:
    dataset_uri: str  # 例: s3://curated/scene-cls/v1
    checkpoint_root: str  # 例: s3://curated/checkpoints
    job_name: str  # TrainJob / RayJob の名前。チェックポイントの置き場所と、MLflow の run を探す目印に使う
    engine: str  # "torchrun"（Kubeflow Trainer）/ "ray"（Ray Train）
    epochs: int = 5
    batch_size: int = 64
    lr: float = 1e-3
    seed: int = 42
    # 前の学習（ジョブ名）のチェックポイントから続けるとき。空なら自分のジョブ名のチェックポイントを探す
    resume_from: str = ""
    experiment: str = "scene-classifier"

    @property
    def checkpoint_job(self) -> str:
        """チェックポイントを読む元のジョブ名。"""
        return self.resume_from or self.job_name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trainer_image() -> str:
    """実行中のイメージ名（ワークフローが環境変数 TRAINER_IMAGE で渡す）。MLflow の系譜のタグに記録する。"""
    return os.environ.get("TRAINER_IMAGE", "unknown")


def configure_threads() -> int:
    """PyTorch が使うスレッド数を、Pod の CPU limit に合わせる。

    何も設定しないと、PyTorch はノードの CPU コア数ぶんのスレッドを作る。Pod の CPU limit が 1 なら、
    スレッドが CPU を取り合って（CFS の制限に引っかかって）かえって遅くなるので、OMP_NUM_THREADS（Pod の env で指定）に合わせる。
    """
    n = int(os.environ.get("OMP_NUM_THREADS", "1"))
    torch.set_num_threads(n)
    try:
        torch.set_num_interop_threads(1)  # 演算どうしの並列は使わない（プロセス内で 1 回しか設定できない）
    except RuntimeError:
        pass
    return n
