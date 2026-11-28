"""画像の埋め込みとベクトル検索（フェーズ 6）。

    register-model … 埋め込みモデル（SigLIP）の重みを、リビジョンを固定して MLflow のモデルレジストリに登録する
    embed          … GOP 索引（フェーズ 5）から各 GOP の先頭フレーム（キーフレーム）だけを Range GET でデコードし、
                     埋め込みを LanceDB の表（s3://vectors/）に入れる。セグメントごとの平均ベクトルの表も作る
    search         … テキスト・画像で検索し、segment_id の一覧（Parquet）を書き出す（indexer select --segment-ids-uri にそのまま渡せる）
    eval           … 正解値と照らし合わせて precision@k を測る
"""

from importlib.metadata import PackageNotFoundError, version

EMBEDDER_NAME = "embedder"

try:
    EMBEDDER_VERSION = version(EMBEDDER_NAME)
except PackageNotFoundError:
    EMBEDDER_VERSION = "0.0.0+unknown"
