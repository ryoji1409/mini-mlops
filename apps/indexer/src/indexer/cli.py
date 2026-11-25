"""コマンドラインの入口。

    indexer build-index [--verify-gops N] [--force]
    indexer tag
    indexer select --dataset NAME --version N --sql night_rain [--frames first|random|all] [--seed N]
                   [--gops-per-shard N] [--segment-ids-uri URI] [--summary-file PATH]
    indexer count-truth [--time-of-day night] [--weather rain]

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

from indexer.build_index import build_index
from indexer.select import FRAME_CHOICES, ManifestConflictError, SelectParams, select_and_curate
from indexer.storage import open_location
from indexer.tag import tag_segments
from indexer.truth import count_segments


def _bool(value: str) -> bool:
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"true / false で指定してください: {value}")


def _uri(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indexer", description="動画入り MCAP の索引・タグ付け・選別")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-index", help="MCAP の summary だけを読んで GOP 索引を作る")
    p.add_argument("--verify-gops", type=int, default=1, help="参照位置の確認のために実際に読む GOP の数（セッションごと、最大 3）")
    # --force だけでも、--force=true / --force=false でも書ける（ワークフローから値で渡せるように）
    p.add_argument("--force", type=_bool, nargs="?", const=True, default=False, help="作成済みでも作り直す")

    sub.add_parser("tag", help="フレームごとの正解値をセグメントごとに集計してタグを付ける")

    p = sub.add_parser("select", help="SQL でセグメントを選び、サンプル一覧（manifest）とシャードを curated に作る")
    p.add_argument("--dataset", required=True, help="データセット名（curated/<名前>/v<N>/）")
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--sql", required=True, help="選別の SQL（sql/ の名前、または .sql ファイルのパス）")
    p.add_argument("--frames", choices=FRAME_CHOICES, default="first", help="GOP のどのフレームをサンプルにするか")
    p.add_argument("--seed", type=int, default=0, help="--frames random のときの乱数の種")
    p.add_argument("--gops-per-shard", type=int, default=50)
    p.add_argument(
        "--segment-ids-uri",
        default=os.environ.get("SEGMENT_IDS_URI") or None,
        help="外から渡す segment_id の一覧（Parquet）。SQL から candidates として使える（環境変数 SEGMENT_IDS_URI でも可）",
    )
    p.add_argument("--summary-file", type=Path, help="結果を JSON で書き出すファイル（Argo の出力用）")

    p = sub.add_parser("count-truth", help="metadata.json だけから、条件に合うセグメント数を数える（選別結果の照合用）")
    p.add_argument("--time-of-day")
    p.add_argument("--weather")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = _parser().parse_args(argv)
    raw_uri, derived_uri, curated_uri = _uri("RAW_URI", "s3://raw"), _uri("DERIVED_URI", "s3://derived"), _uri("CURATED_URI", "s3://curated")

    if args.command == "build-index":
        r = build_index(raw_uri, derived_uri, verify=min(args.verify_gops, 3), force=args.force)
        print(json.dumps({"indexed": len(r.indexed), "skipped": len(r.skipped), "bytes_read": r.bytes_read, "mcap_bytes": r.mcap_bytes}))
    elif args.command == "tag":
        table = tag_segments(open_location(raw_uri), open_location(derived_uri), raw_uri, derived_uri)
        print(json.dumps({"segments": table.num_rows}))
    elif args.command == "select":
        params = SelectParams(args.dataset, args.version, args.sql, args.frames, args.seed, args.gops_per_shard, args.segment_ids_uri)
        try:
            m = select_and_curate(raw_uri, derived_uri, open_location(curated_uri), params)
        except ManifestConflictError as e:
            logging.getLogger("indexer").error("%s", e)
            return 1
        summary = {"manifest": f"{curated_uri}/{args.dataset}/v{args.version}/manifest.json", **m["counts"]}
        print(json.dumps(summary, ensure_ascii=False))
        if args.summary_file:
            args.summary_file.write_text(json.dumps(summary) + "\n")
    elif args.command == "count-truth":
        matched, total = count_segments(open_location(raw_uri), args.time_of_day, args.weather)
        print(json.dumps({"matched_segments": matched, "video_segments": total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
