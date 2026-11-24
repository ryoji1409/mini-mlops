"""Ray Train（TorchTrainer）で起動する学習（KubeRay の RayJob から）。

RayJob を作ると、KubeRay が Ray クラスタ（head 1 + worker N）を立て、head の上で
    trainer train-ray --num-workers N ...
が動く（ドライバ）。ドライバは TorchTrainer に「各ワーカーで動かす関数（_train_loop）」を渡すだけで、
DDP の準備（プロセスグループの作成・gloo の接続・DistributedDataParallel で包む）は Ray Train がやる。

torchrun 版との違い:
- データ: Ray Data が Parquet を読み、行をワーカーに均等に配る（get_dataset_shard）。読み込み・デコードを学習と並行して進められる
- チェックポイント: ray.train.report(checkpoint=...) で渡すと、Ray Train が storage_path に保存する
- 再開: ワーカーが落ちると、Ray Train が最後のチェックポイントからワーカーを作り直して続ける（FailureConfig.max_failures）。
        別の RayJob から続けたいときは、同じ名前（RunConfig.name）で起動すると、保存済みのチェックポイントから再開する
"""

from __future__ import annotations

import json
import logging
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import ray
import ray.train
import ray.train.torch
import torch
import torch.distributed as dist

from trainer.data import decode_images, load_manifest, shard_uris
from trainer.engine import EvalSums, evaluate, train_epoch
from trainer.model import SceneNet
from trainer.settings import TrainSettings, configure_threads
from trainer.storage import s3_filesystem
from trainer.tracking import Tracker

log = logging.getLogger(__name__)

STATE_FILE = "state.pt"


def _to_batches(batches: Any):
    """Ray Data のバッチ（numpy の辞書）を、学習用の（画像, ラベル, ラベル）に変換する。"""
    for b in batches:
        yield (
            decode_images(list(b["image"])),
            torch.from_numpy(b["time_of_day"].astype(np.int64)),
            torch.from_numpy(b["weather"].astype(np.int64)),
        )


def _train_loop(config: dict[str, Any]) -> None:
    """各ワーカー（rank）で動く関数。Ray Train が num_workers 個のプロセスで同時に呼ぶ。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = TrainSettings(**config["settings"])
    manifest = config["manifest"]
    ctx = ray.train.get_context()
    rank, world_size = ctx.get_world_rank(), ctx.get_world_size()
    threads = configure_threads()
    # node = Ray のノード ID（Pod ごとに違う）。rank が別々の Pod に置かれているかを確かめられる
    node = ray.get_runtime_context().get_node_id()[:8]
    log.info("rank=%d world_size=%d host=%s node=%s threads=%d engine=ray", rank, world_size, socket.gethostname(), node, threads)

    torch.manual_seed(settings.seed)
    model = SceneNet()
    # DistributedDataParallel で包む（プロセスグループは Ray Train が gloo で作ってある）
    ddp = ray.train.torch.prepare_model(model)
    optimizer = torch.optim.Adam(ddp.parameters(), lr=settings.lr)

    # 再開: Ray Train が渡してくる最新のチェックポイント（落ちて作り直されたとき・同じ名前で起動し直したとき）
    start_epoch, run_id = 0, None
    ckpt = ray.train.get_checkpoint()
    if ckpt is not None:
        with ckpt.as_directory() as d:
            state = torch.load(Path(d) / STATE_FILE, map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        start_epoch, run_id = state["epoch"], state.get("mlflow_run_id")
        log.info("rank=%d: チェックポイントから再開します（%d エポック終了済み）", rank, start_epoch)

    tracker = Tracker(enabled=rank == 0)
    tracker.start(settings, manifest, world_size, run_id)

    train_shard = ray.train.get_dataset_shard("train")
    val_shard = ray.train.get_dataset_shard("val")
    sums = EvalSums()
    for epoch in range(start_epoch, settings.epochs):
        started = time.monotonic()
        # drop_last=True: 端数のバッチを捨てる。Ray Data は行を均等に配るので、全 rank のバッチ数が揃う
        batches = train_shard.iter_batches(
            batch_size=settings.batch_size, batch_format="numpy", drop_last=True,
            local_shuffle_buffer_size=settings.batch_size * 8, local_shuffle_seed=settings.seed + epoch,
        )
        loss_sum, n = train_epoch(ddp, optimizer, _to_batches(batches))
        train_seconds = time.monotonic() - started
        totals = torch.tensor([loss_sum, float(n)], dtype=torch.float64)
        dist.all_reduce(totals)

        local = evaluate(model, _to_batches(val_shard.iter_batches(batch_size=settings.batch_size, batch_format="numpy")))
        t = local.to_tensor()
        dist.all_reduce(t)
        sums = EvalSums.from_tensor(t)
        metrics = {"train_loss": float(totals[0] / max(totals[1], 1)), "epoch_train_seconds": train_seconds} | sums.metrics()
        log.info("rank=%d epoch=%d/%d %s", rank, epoch + 1, settings.epochs, {k: round(v, 4) for k, v in metrics.items()})

        # 全 rank が report を呼ぶ（Ray Train の決まり）。チェックポイントを渡すのは rank 0 だけ
        if rank == 0:
            tracker.log_epoch(epoch + 1, metrics)
            with tempfile.TemporaryDirectory() as d:
                state = {"epoch": epoch + 1, "model": model.state_dict(), "optimizer": optimizer.state_dict(), "mlflow_run_id": tracker.run_id}
                torch.save(state, Path(d) / STATE_FILE)
                ray.train.report(metrics, checkpoint=ray.train.Checkpoint.from_directory(d))
        else:
            ray.train.report(metrics)

    if sums.n == 0:
        local = evaluate(model, _to_batches(val_shard.iter_batches(batch_size=settings.batch_size, batch_format="numpy")))
        t = local.to_tensor()
        dist.all_reduce(t)
        sums = EvalSums.from_tensor(t)
    if rank == 0:
        tracker.finish(model, sums, manifest["labels"])


def _storage(settings: TrainSettings) -> tuple[str, Any]:
    """チェックポイントの置き場所。S3 なら「バケット/パス」と pyarrow の S3FileSystem を、ローカルならパスだけを返す。"""
    root = settings.checkpoint_root.rstrip("/")
    if root.startswith("s3://"):
        return root.removeprefix("s3://") + "/ray", s3_filesystem()
    return root + "/ray", None


def _read_parquet(uris: list[str]) -> ray.data.Dataset:
    if uris[0].startswith("s3://"):
        return ray.data.read_parquet([u.removeprefix("s3://") for u in uris], filesystem=s3_filesystem(), columns=["image", "time_of_day", "weather"])
    return ray.data.read_parquet(uris, columns=["image", "time_of_day", "weather"])


def run(settings: TrainSettings, num_workers: int, placement_strategy: str = "SPREAD") -> dict[str, Any]:
    """ドライバ（RayJob の entrypoint）。学習が終わるまで待ち、最後のメトリクスを返す。

    placement_strategy: 学習ワーカーを Ray のノード（= worker の Pod）にどう置くか。
      - STRICT_SPREAD: 必ず別々の Pod に置く（クラスタではこれを使う。ワークフローから渡す）
      - SPREAD       : できるだけ別々に置く（手元のテストのように Ray が 1 台だけでも動く。既定）
      - PACK         : なるべく同じ Pod にまとめる（Ray Train の既定）
    worker の Pod は「CPU 2 つ」と名乗っている（学習 1 + Ray Data の読み込み 1）ので、PACK のままだと
    1 つの Pod に学習ワーカーが 2 つ入ってしまい、メモリ不足で Ray に強制終了された（フェーズ 4 の確認で実際に起きた）。
    """
    from ray.train.torch import TorchConfig, TorchTrainer

    ray.init(ignore_reinit_error=True)
    manifest = load_manifest(settings.dataset_uri)
    storage_path, storage_fs = _storage(settings)
    trainer = TorchTrainer(
        train_loop_per_worker=_train_loop,
        train_loop_config={"settings": settings.to_dict(), "manifest": manifest},
        # ワーカー 1 つに CPU 1。use_gpu=False（CPU だけ）
        scaling_config=ray.train.ScalingConfig(
            num_workers=num_workers, use_gpu=False, resources_per_worker={"CPU": 1}, placement_strategy=placement_strategy
        ),
        torch_config=TorchConfig(backend="gloo"),
        datasets={"train": _read_parquet(shard_uris(manifest, "train")), "val": _read_parquet(shard_uris(manifest, "val"))},
        run_config=ray.train.RunConfig(
            # 同じ名前で起動し直すと、保存済みのチェックポイントから再開する（resume_from に前のジョブ名を渡す）
            name=settings.checkpoint_job,
            storage_path=storage_path,
            storage_filesystem=storage_fs,
            # ワーカーが落ちたら、最後のチェックポイントから 2 回まで自動でやり直す
            failure_config=ray.train.FailureConfig(max_failures=2),
            checkpoint_config=ray.train.CheckpointConfig(num_to_keep=2),
        ),
    )
    result = trainer.fit()
    log.info("学習が終わりました: %s", json.dumps(result.metrics, default=str))
    if result.error:
        raise result.error
    return dict(result.metrics or {})


