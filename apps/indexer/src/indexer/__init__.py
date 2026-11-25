"""動画入り MCAP の索引・タグ付け・選別（フェーズ 5）。

    build-index … MCAP の summary（チャンクの索引）だけを S3 の Range GET で読み、GOP ごとの「MCAP 参照位置」を Parquet にする
    tag         … フレームごとの正解値（raw の ground_truth.parquet）をセグメントごとに集計し、タグを付ける
    select      … DuckDB の SQL（sql/*.sql）でセグメントを選び、サンプル一覧（manifest）とシャードを curated に作る
"""

from importlib.metadata import PackageNotFoundError, version

INDEXER_NAME = "indexer"

try:
    INDEXER_VERSION = version(INDEXER_NAME)
except PackageNotFoundError:
    INDEXER_VERSION = "0.0.0+unknown"
