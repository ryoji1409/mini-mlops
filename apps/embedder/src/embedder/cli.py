"""コマンドラインの入口。

    embedder register-model [--hf-repo R] [--hf-revision SHA] [--name N] [--alias A] [--model-dir DIR]
    embedder embed [--gop-stride N] [--batch-size N] [--prefetch N] [--force]
    embedder search (--text T | --image PATH) [--top-k K] [--min-score S] [--where SQL] [--level segments|frames]
                    [--out URI] [--preview-dir DIR] [--preview-count N]
    embedder eval [--k 10] [--query T --truth SQL ...] [--summary-file PATH]

モデルの指定（embed / search / eval 共通）:
    既定は MLflow のモデルレジストリの --model-name / --model-alias（EMBEDDER_MODEL_NAME / EMBEDDER_MODEL_ALIAS）。
    重みは --model-cache（EMBEDDER_MODEL_CACHE。既定 ~/.cache/mini-mlops/models）にダウンロードして使い回す。
    --model-dir を付けると、MLflow を通さずにそのディレクトリの重みを使う（mini-mlops-model.json が必要）。
入力・出力の場所は環境変数で指定する（既定は s3://raw、s3://derived、s3://vectors）:
    RAW_URI / DERIVED_URI / VECTORS_URI、S3 の接続先とキーは aws CLI と同じ AWS_* 環境変数、MLflow は MLFLOW_TRACKING_URI
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from embedder.registry import ModelRef, local_model, register_model, resolve_model

# 第一候補のモデル（Google、Apache-2.0）。リビジョンは Hugging Face のコミットのハッシュで固定する
DEFAULT_HF_REPO = "google/siglip-base-patch16-224"
DEFAULT_HF_REVISION = "7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed"
DEFAULT_MODEL_NAME = "siglip-base-p16-224"
DEFAULT_ALIAS = "embedding"


def _bool(value: str) -> bool:
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"true / false で指定してください: {value}")


def _uri(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


def _add_model_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model-name", default=os.environ.get("EMBEDDER_MODEL_NAME", DEFAULT_MODEL_NAME))
    p.add_argument("--model-alias", default=os.environ.get("EMBEDDER_MODEL_ALIAS", DEFAULT_ALIAS))
    p.add_argument("--model-cache", type=Path, default=Path(os.environ.get("EMBEDDER_MODEL_CACHE", Path.home() / ".cache" / "mini-mlops" / "models")))
    p.add_argument("--model-dir", type=Path, help="MLflow を通さずに使う重みのディレクトリ")
    p.add_argument("--threads", type=int, default=int(os.environ.get("EMBEDDER_THREADS", "0")) or None, help="torch のスレッド数（Pod の CPU 数に合わせる）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="embedder", description="画像の埋め込みとベクトル検索")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register-model", help="モデルの重みを（リビジョンを固定して）MLflow のモデルレジストリに登録する")
    p.add_argument("--hf-repo", default=DEFAULT_HF_REPO)
    p.add_argument("--hf-revision", default=DEFAULT_HF_REVISION)
    p.add_argument("--name", default=DEFAULT_MODEL_NAME)
    p.add_argument("--alias", default=DEFAULT_ALIAS)
    p.add_argument("--model-dir", type=Path, help="Hugging Face から取らずに、このディレクトリの重みを登録する")
    p.add_argument("--model-cache", type=Path, default=Path(os.environ.get("EMBEDDER_MODEL_CACHE", Path.home() / ".cache" / "mini-mlops" / "models")),
                   help="登録した重みを、このキャッシュにも置く（後の search / eval がダウンロードし直さない）")

    p = sub.add_parser("embed", help="キーフレームを埋め込んで LanceDB の表に入れる")
    _add_model_args(p)
    p.add_argument("--gop-stride", type=int, default=1, help="N 個おきの GOP だけ埋め込む（1 = 全部、GOP は 1 秒）")
    p.add_argument("--batch-size", type=int, default=8, help="まとめて埋め込む枚数")
    p.add_argument("--prefetch", type=int, default=4, help="先に読んでデコードしておく GOP の数")
    p.add_argument("--force", type=_bool, nargs="?", const=True, default=False, help="埋め込み済みでも作り直す")
    p.add_argument("--summary-file", type=Path)

    p = sub.add_parser("search", help="テキスト・画像で検索して、segment_id の一覧を書き出す")
    _add_model_args(p)
    p.add_argument("--text")
    p.add_argument("--image", help="この画像（ローカルのパス、または s3://…）に似たシーンを探す")
    p.add_argument("--prompt", default=None, help="テキストを埋め込むときの文の型（既定 'This is a photo of {}.'）")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--min-score", type=float, help="これより低いスコアは捨てる（テキストは確率 0〜1、画像はコサイン類似度）")
    p.add_argument("--where", help="表の列への条件（例: \"weather = 'rain'\"）")
    p.add_argument("--level", choices=("segments", "frames"), default="segments")
    p.add_argument("--out", help="結果（segment_id の Parquet）の書き出し先。indexer select --segment-ids-uri にそのまま渡せる")
    p.add_argument("--preview-dir", type=Path, help="上位のキーフレームを PNG で保存するディレクトリ")
    p.add_argument("--preview-count", type=int, default=5)

    p = sub.add_parser("eval", help="正解値と照らし合わせて precision@k を測る")
    _add_model_args(p)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--query", action="append", default=[], help="クエリ（--truth と同じ数だけ。省略すると既定の組）")
    p.add_argument("--truth", action="append", default=[], help="正解の条件（segments 表の列への SQL）")
    p.add_argument("--summary-file", type=Path)
    return parser


def _model(args: argparse.Namespace) -> ModelRef:
    if args.model_dir:
        return local_model(args.model_dir)
    return resolve_model(args.model_name, args.model_alias, args.model_cache)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = _parser().parse_args(argv)
    raw_uri, derived_uri, vectors_uri = _uri("RAW_URI", "s3://raw"), _uri("DERIVED_URI", "s3://derived"), _uri("VECTORS_URI", "s3://vectors")

    if args.command == "register-model":
        print(json.dumps(register_model(args.hf_repo, args.hf_revision, args.name, args.alias, args.model_dir, args.model_cache), ensure_ascii=False))
        return 0

    # ここから先は torch・transformers を使う（register-model だけなら読み込まない）
    from embedder.model import DEFAULT_PROMPT, SiglipEmbedder

    embedder = SiglipEmbedder(_model(args), threads=args.threads)
    if args.command == "embed":
        from embedder.embed import embed

        report = embed(raw_uri, derived_uri, vectors_uri, embedder, args.gop_stride, args.batch_size, args.prefetch, args.force)
        summary = report.summary()
        print(json.dumps(summary, ensure_ascii=False))
        if args.summary_file:
            args.summary_file.write_text(json.dumps(summary, ensure_ascii=False) + "\n")
    elif args.command == "search":
        from embedder.search import Query, Searcher, SearchParams, format_results, query_vector, save_previews, write_result

        searcher = Searcher(vectors_uri, embedder)
        query = Query(text=args.text, image=args.image, prompt=args.prompt or DEFAULT_PROMPT)
        params = SearchParams(args.top_k, args.min_score, args.where, args.level)
        vector = query_vector(embedder, query)
        results = searcher.search(query, params, vector=vector)
        print(format_results(results))
        logging.getLogger("embedder").info("検索: %s → %d 件（%s、上位 %d、しきい値 %s、条件 %s）", query.describe(), len(results), params.level, params.top_k, params.min_score, params.where)
        if args.out:
            write_result(results, args.out, query, params, embedder)
            logging.getLogger("embedder").info("segment_id の一覧を書きました: %s", args.out)
        if args.preview_dir:
            paths = save_previews(searcher, results, vector, args.preview_dir, args.preview_count)
            logging.getLogger("embedder").info("上位 %d 件のキーフレームを %s に保存しました", len(paths), args.preview_dir)
    elif args.command == "eval":
        from embedder.evaluate import DEFAULT_QUERIES, EvalCase, evaluate, format_table
        from embedder.search import Searcher

        if len(args.query) != len(args.truth):
            raise SystemExit("--query と --truth は同じ数だけ指定してください")
        cases = [EvalCase(q, t) for q, t in zip(args.query, args.truth, strict=True)] or list(DEFAULT_QUERIES)
        rows = evaluate(Searcher(vectors_uri, embedder), cases, args.k)
        print(format_table(rows, args.k))
        if args.summary_file:
            args.summary_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
