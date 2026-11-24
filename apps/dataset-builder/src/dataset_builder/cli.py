"""コマンドラインの入口。

    dataset-builder build --version 1 [--summary-file PATH]

入力・出力の場所は環境変数で指定する（既定は s3://raw、s3://derived、s3://curated）:
    RAW_URI / DERIVED_URI / CURATED_URI、S3 の接続先とキーは aws CLI と同じ AWS_* 環境変数
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dataset_builder.build import BuildParams, DatasetConflictError, build_dataset
from dataset_builder.storage import open_location


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dataset-builder", description="学習用データセット（scene-cls）を作る")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="データセットを作る（同じ中身が既にあればスキップ）")
    defaults = BuildParams(version=0)
    p.add_argument("--version", type=int, required=True, help="データセットのバージョン番号（v<N> の N）")
    p.add_argument("--width", type=int, default=defaults.width)
    p.add_argument("--height", type=int, default=defaults.height)
    p.add_argument("--jpeg-quality", type=int, default=defaults.jpeg_quality)
    p.add_argument("--val-per-combo", type=int, default=defaults.val_per_combo, help="（時間帯, 天候）の組ごとの検証用セッション数")
    p.add_argument("--train-shards", type=int, default=defaults.train_shards)
    p.add_argument("--val-shards", type=int, default=defaults.val_shards)
    p.add_argument("--seed", type=int, default=defaults.seed)
    p.add_argument("--summary-file", type=Path, help="結果（manifest の URI とハッシュ）を JSON で書き出すファイル（Argo の出力用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = _parser().parse_args(argv)
    params = BuildParams(
        version=args.version,
        width=args.width,
        height=args.height,
        jpeg_quality=args.jpeg_quality,
        val_per_combo=args.val_per_combo,
        train_shards=args.train_shards,
        val_shards=args.val_shards,
        seed=args.seed,
    )
    raw = open_location(os.environ.get("RAW_URI", "s3://raw"))
    derived = open_location(os.environ.get("DERIVED_URI", "s3://derived"))
    curated = open_location(os.environ.get("CURATED_URI", "s3://curated"))
    try:
        result = build_dataset(raw, derived, curated, params)
    except DatasetConflictError as e:
        logging.getLogger("dataset_builder").error("%s", e)
        return 1
    summary = {"status": result.status, "manifest": result.manifest_uri, "dataset_hash": result.dataset_hash}
    print(json.dumps(summary, ensure_ascii=False))
    if args.summary_file:
        args.summary_file.write_text(json.dumps(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
