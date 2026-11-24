"""torchrun で起動する学習（Kubeflow Trainer の TrainJob から）。

TrainJob を作ると、Kubeflow Trainer が JobSet → Job → Pod を作り、各 Pod で次のように起動する:
    torchrun --no-python trainer train-torchrun ...
torchrun は、Trainer が入れた環境変数 PET_NNODES（ノード数）/ PET_NODE_RANK（自分の番号）/
PET_MASTER_ADDR・PET_MASTER_PORT（rank 0 の場所）を読んで、プロセスどうしをつなぐ。
その後このコードが init_process_group("gloo") で、CPU 用の通信方式 gloo を使った DDP を始める。

DDP（DistributedDataParallel）: 各 rank が同じモデルを持ち、別々のデータで勾配を計算し、
backward のたびに全 rank の勾配を平均（all-reduce）してから更新する。結果として全 rank のモデルは常に同じになる。
"""

from __future__ import annotations

import logging
import socket
import time

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel

from trainer import checkpoint
from trainer.data import Frames, assign_shards, iter_batches, load_manifest, num_train_batches, read_frames, shard_uris
from trainer.engine import EvalSums, evaluate, train_epoch
from trainer.model import SceneNet
from trainer.settings import TrainSettings, configure_threads
from trainer.tracking import Tracker

log = logging.getLogger(__name__)


def _all_reduce(t: torch.Tensor, op: dist.ReduceOp = dist.ReduceOp.SUM) -> torch.Tensor:
    dist.all_reduce(t, op=op)
    return t


def _val_frames(manifest: dict, rank: int, world_size: int) -> Frames:
    """検証用のファイルも rank で分担する。ファイル数よりワーカーが多いときは、担当なし（0 件）の rank があってよい。"""
    uris = shard_uris(manifest, "val")[rank::world_size]
    return read_frames(uris) if uris else Frames([], torch.empty(0).long().numpy(), torch.empty(0).long().numpy())


def run(settings: TrainSettings) -> None:
    dist.init_process_group(backend="gloo")  # CPU なので NCCL ではなく gloo
    rank, world_size = dist.get_rank(), dist.get_world_size()
    threads = configure_threads()
    log.info("rank=%d world_size=%d host=%s threads=%d engine=torchrun", rank, world_size, socket.gethostname(), threads)

    manifest = load_manifest(settings.dataset_uri)
    train = read_frames(assign_shards(shard_uris(manifest, "train"), rank, world_size))
    val = _val_frames(manifest, rank, world_size)
    log.info("rank=%d: 学習 %d フレーム、検証 %d フレームを担当", rank, len(train), len(val))

    torch.manual_seed(settings.seed)  # 全 rank で同じ初期値（DDP は rank 0 の重みを他にも配るが、念のため揃える）
    model = SceneNet()
    ddp = DistributedDataParallel(model)
    optimizer = torch.optim.Adam(ddp.parameters(), lr=settings.lr)

    # 再開: チェックポイントがあれば、全 rank がそれを読む（全 rank が同じ状態から始まる）
    start_epoch, run_id = 0, None
    state = checkpoint.load_latest(settings.checkpoint_root, settings.checkpoint_job)
    if state is not None:
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        start_epoch, run_id = state["epoch"], state.get("mlflow_run_id")

    tracker = Tracker(enabled=rank == 0)
    tracker.start(settings, manifest, world_size, run_id)

    # 全 rank のバッチ数を、いちばん少ない rank に揃える。
    # 揃えないと、多い rank が backward の all-reduce で、もう来ない相手を待ち続けて止まる
    n_batches = int(_all_reduce(torch.tensor(num_train_batches(train, settings.batch_size)), dist.ReduceOp.MIN))
    log.info("rank=%d: 1 エポック %d バッチ（全 rank で揃えた数）", rank, n_batches)

    sums = EvalSums()
    for epoch in range(start_epoch, settings.epochs):
        started = time.monotonic()
        batches = iter_batches(train, settings.batch_size, shuffle=True, seed=settings.seed, epoch=epoch, max_batches=n_batches)
        loss_sum, n = train_epoch(ddp, optimizer, batches)
        train_seconds = time.monotonic() - started
        totals = _all_reduce(torch.tensor([loss_sum, float(n)], dtype=torch.float64))

        # 検証は各 rank が自分の担当分を評価し、合計（混同行列の件数）を全 rank で足し合わせる
        local = evaluate(model, iter_batches(val, settings.batch_size, shuffle=False, seed=settings.seed, epoch=epoch))
        sums = EvalSums.from_tensor(_all_reduce(local.to_tensor()))
        metrics = {"train_loss": float(totals[0] / max(totals[1], 1)), "epoch_train_seconds": train_seconds} | sums.metrics()
        log.info("rank=%d epoch=%d/%d %s", rank, epoch + 1, settings.epochs, {k: round(v, 4) for k, v in metrics.items()})

        if rank == 0:
            tracker.log_epoch(epoch + 1, metrics)
            uri = checkpoint.save(
                settings.checkpoint_root,
                settings.job_name,
                {"epoch": epoch + 1, "model": model.state_dict(), "optimizer": optimizer.state_dict(), "mlflow_run_id": tracker.run_id, "metrics": metrics},
            )
            log.info("チェックポイントを保存しました: %s", uri)
        dist.barrier()  # rank 0 の保存が終わるまで、次のエポックに進まない

    if sums.n == 0:
        # 再開したら全エポックが終わっていた場合など、このプロセスで一度も評価していなければ、ここで評価する
        local = evaluate(model, iter_batches(val, settings.batch_size, shuffle=False, seed=settings.seed, epoch=0))
        sums = EvalSums.from_tensor(_all_reduce(local.to_tensor()))
    if rank == 0:
        tracker.finish(model, sums, manifest["labels"])
    dist.barrier()
    dist.destroy_process_group()
