"""学習器のテスト。ローカルの CPU で、本当に 2 プロセスの DDP（gloo）を動かす。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import torch

from trainer.data import assign_shards, read_frames, shard_uris, load_manifest, iter_batches
from trainer.engine import EvalSums
from trainer.tracking import confusion_png

APP_DIR = Path(__file__).resolve().parents[1]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MLFLOW_TRACKING_URI", None)  # テストでは MLflow に記録しない
    env["OMP_NUM_THREADS"] = "1"
    env["MLFLOW_DISABLE_AGENT_HINT"] = "1"
    # `uv run` の中で Ray を起動すると、Ray はワーカーでも uv run をやり直す（作業ディレクトリを一時的な場所に複製して）。
    # 複製先では pyproject.toml の ../indexer などの相対パスが解決できず、ワーカーが起動しないので、この動きを止める。
    # 学習器の Python 環境はもう揃っているので、ワーカーは同じ Python をそのまま使えばよい
    env["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
    return env


def _torchrun(dataset: Path, ckpt: Path, job: str, epochs: int, nproc: int = 2) -> subprocess.CompletedProcess:
    torchrun = shutil.which("torchrun", path=str(Path(sys.executable).parent))
    with socket.socket() as s:  # 空いているポートを 1 つ選ぶ
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # クラスタでは Kubeflow Trainer が PET_* 環境変数でノード数・rank 0 の場所を渡す。ここでは引数で同じことをする
    return subprocess.run(
        [torchrun, "--nnodes=1", f"--nproc-per-node={nproc}", "--master-addr=127.0.0.1", f"--master-port={port}", "--no-python", "trainer", "train-torchrun",
         "--dataset-uri", str(dataset), "--checkpoint-root", str(ckpt), "--job-name", job, "--epochs", str(epochs), "--batch-size", "32"],
        env=_env(), capture_output=True, text=True, timeout=300,
    )


def test_ファイルの分担とバッチ(dataset_dir: Path) -> None:
    manifest = load_manifest(str(dataset_dir))
    uris = shard_uris(manifest, "train")
    assert assign_shards(uris, 0, 2) == uris[0::2] and assign_shards(uris, 1, 2) == uris[1::2]
    with pytest.raises(ValueError):
        assign_shards(uris, 0, 8)  # ファイルより多いワーカーには分けられない
    frames = read_frames(uris[:1])
    x, y_tod, y_w = next(iter_batches(frames, 16, shuffle=True, seed=1, epoch=0))
    assert x.shape == (16, 3, 90, 160) and 0.0 <= float(x.min()) and float(x.max()) <= 1.0
    # 同じ seed・同じエポックなら同じ順番（再開しても同じ並び）
    again = next(iter_batches(frames, 16, shuffle=True, seed=1, epoch=0))
    assert torch.equal(y_tod, again[1])


def test_評価の集計を足し合わせられる() -> None:
    a = EvalSums(loss_sum=2.0, n=4)
    a.confusion_tod[0, 0] = 3
    a.confusion_tod[1, 2] = 1
    a.confusion_weather[1, 1] = 4
    b = EvalSums.from_tensor(a.to_tensor() * 2)  # 2 rank 分を足したのと同じ
    assert b.n == 8 and int(b.confusion_tod[0, 0]) == 6
    m = b.metrics()
    assert m["val_time_of_day_accuracy"] == 0.75 and m["val_weather_accuracy"] == 1.0
    assert confusion_png(b, {"time_of_day": ["day", "dusk", "night"], "weather": ["clear", "rain"]}).startswith(b"\x89PNG")


def test_torchrunで2プロセスのDDPが完走し途中から再開できる(dataset_dir: Path, tmp_path: Path) -> None:
    ckpt = tmp_path / "ckpt"
    first = _torchrun(dataset_dir, ckpt, "job-a", epochs=1)
    assert first.returncode == 0, first.stderr[-3000:]
    # 両方の rank のログに world_size=2 が出る
    assert "rank=0 world_size=2" in first.stderr and "rank=1 world_size=2" in first.stderr
    assert json.loads((ckpt / "job-a" / "latest.json").read_text())["epoch"] == 1

    # 同じジョブ名で epochs=3 にして起動し直すと、1 エポック目のチェックポイントから続ける（2, 3 エポック目だけ動く）
    second = _torchrun(dataset_dir, ckpt, "job-a", epochs=3)
    assert second.returncode == 0, second.stderr[-3000:]
    assert "1 エポック終了済み" in second.stderr
    assert "epoch=1/3" not in second.stderr and "epoch=3/3" in second.stderr
    # 色で分かれる簡単なデータなので、エポックが進むと学習の損失が下がる
    loss = lambda e: torch.load(ckpt / "job-a" / f"epoch-{e:04d}.pt", weights_only=False)["metrics"]["train_loss"]  # noqa: E731
    assert loss(3) < loss(1)


def test_RayTrainで2ワーカーが完走する(dataset_dir: Path, tmp_path: Path) -> None:
    code = (
        "import logging, ray; logging.basicConfig(level=logging.INFO);"
        "ray.init(num_cpus=4, include_dashboard=False);"
        "from trainer import ray_train; from trainer.settings import TrainSettings;"
        f"s = TrainSettings(dataset_uri={str(dataset_dir)!r}, checkpoint_root={str(tmp_path / 'ckpt')!r}, job_name='ray-a', engine='ray', epochs=2, batch_size=32);"
        "m = ray_train.run(s, num_workers=2); print('RESULT', m['val_weather_accuracy'])"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=_env(), capture_output=True, text=True, timeout=900)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-4000:]
    assert "RESULT" in proc.stdout
    assert "rank=0 world_size=2" in out and "rank=1 world_size=2" in out
    # 置き方の指定（既定の SPREAD）が Ray Train に渡っていること（手元は Ray が 1 台なので、SPREAD でも同じ場所に置かれる）
    # Ray Train のチェックポイントが storage_path（<checkpoint_root>/ray/<ジョブ名>）に保存されている
    assert list((tmp_path / "ckpt" / "ray" / "ray-a").rglob("state.pt"))
