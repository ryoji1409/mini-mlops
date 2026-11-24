"""コマンドラインの入口。

    trainer train-torchrun [学習の引数]                  … TrainJob の各 Pod で torchrun から起動される
    trainer train-ray --num-workers N [学習の引数]        … RayJob の entrypoint（head の上のドライバ）
    trainer register --job-name X --threshold 0.8        … 評価して、閾値を超えたらモデルを登録
    trainer trace --model-name scene-classifier --alias candidate   … 系譜をたどる
    trainer wait-trainjob --namespace training --name X   … TrainJob の完了を待つ

学習の引数の既定値は環境変数からも渡せる（ワークフローの Pod では env で渡す）:
    DATASET_URI、CHECKPOINT_ROOT（既定 s3://curated/checkpoints）、JOB_NAME
MLflow の場所は MLFLOW_TRACKING_URI、S3 は aws CLI と同じ AWS_* 環境変数。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from trainer.settings import TrainSettings


def _add_train_args(p: argparse.ArgumentParser) -> None:
    defaults = TrainSettings(dataset_uri="", checkpoint_root="", job_name="", engine="")
    p.add_argument("--dataset-uri", default=os.environ.get("DATASET_URI"), help="例: s3://curated/scene-cls/v1")
    p.add_argument("--checkpoint-root", default=os.environ.get("CHECKPOINT_ROOT", "s3://curated/checkpoints"))
    p.add_argument("--job-name", default=os.environ.get("JOB_NAME"), help="TrainJob / RayJob の名前（チェックポイントの置き場所・MLflow の目印）")
    p.add_argument("--epochs", type=int, default=defaults.epochs)
    p.add_argument("--batch-size", type=int, default=defaults.batch_size)
    p.add_argument("--lr", type=float, default=defaults.lr)
    p.add_argument("--seed", type=int, default=defaults.seed)
    p.add_argument("--resume-from", default="", help="前の学習のジョブ名。そのチェックポイントから続ける（空なら自分のジョブ名で探す）")


def _settings(args: argparse.Namespace, engine: str) -> TrainSettings:
    if not args.dataset_uri or not args.job_name:
        raise SystemExit("--dataset-uri と --job-name（または環境変数 DATASET_URI / JOB_NAME）が必要です")
    return TrainSettings(
        dataset_uri=args.dataset_uri,
        checkpoint_root=args.checkpoint_root,
        job_name=args.job_name,
        engine=engine,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        seed=args.seed,
        resume_from=args.resume_from,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainer", description="シーン分類モデルの学習・登録・系譜の確認")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("train-torchrun", help="torchrun から起動される学習（Kubeflow Trainer）")
    _add_train_args(p)
    p = sub.add_parser("train-ray", help="Ray Train での学習（RayJob の entrypoint）")
    _add_train_args(p)
    p.add_argument("--num-workers", type=int, default=2)
    p.add_argument(
        "--placement-strategy",
        default="SPREAD",
        choices=["STRICT_SPREAD", "SPREAD", "PACK"],
        help="学習ワーカーの置き方。クラスタでは STRICT_SPREAD（必ず別々の Pod）",
    )

    p = sub.add_parser("register", help="評価して、閾値を超えたらモデルレジストリに登録する")
    p.add_argument("--job-name", required=True)
    p.add_argument("--threshold", type=float, required=True, help="検証の正解率（2 つのヘッドの低いほう）の閾値")
    p.add_argument("--experiment", default="scene-classifier")
    p.add_argument("--model-name", default="scene-classifier")
    p.add_argument("--alias", default="candidate")
    p.add_argument("--summary-file", type=Path)

    p = sub.add_parser("trace", help="登録されたモデルから raw の MCAP まで系譜をたどる")
    p.add_argument("--model-name", default="scene-classifier")
    p.add_argument("--alias", default="candidate")
    p.add_argument("--version", default=None, help="エイリアスの代わりにバージョン番号で指定する")
    p.add_argument("--limit", type=int, default=5, help="表示するセッション数（0 で全部）")

    p = sub.add_parser("wait-trainjob", help="TrainJob の完了を待つ")
    p.add_argument("--namespace", default="training")
    p.add_argument("--name", required=True)
    p.add_argument("--timeout-seconds", type=int, default=6 * 3600)
    p.add_argument("--interval", type=int, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = _parser().parse_args(argv)
    if args.command == "train-torchrun":
        from trainer import torchrun_train

        torchrun_train.run(_settings(args, "torchrun"))
        return 0
    if args.command == "train-ray":
        from trainer import ray_train

        ray_train.run(_settings(args, "ray"), args.num_workers, args.placement_strategy)
        return 0
    if args.command == "register":
        from trainer.register import register

        return register(args.job_name, args.threshold, args.experiment, args.model_name, args.alias, args.summary_file)
    if args.command == "trace":
        from trainer.trace import trace

        result = trace(args.model_name, args.alias, args.version, args.limit)
        return 0 if result["dataset_hash_ok"] and all(s["derived_manifest_ok"] for s in result["sessions"]) else 1
    if args.command == "wait-trainjob":
        from trainer.wait import wait_trainjob

        return wait_trainjob(args.namespace, args.name, args.timeout_seconds, args.interval)
    return 2


if __name__ == "__main__":
    sys.exit(main())
