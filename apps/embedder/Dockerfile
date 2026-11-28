# 埋め込みの生成と検索のコンテナイメージ。Argo のワークフローから batch/v1 Job（Kueue の data-processing キュー経由）として動く。
# PyTorch（CPU 版）と transformers・LanceDB が入る。Ray は使わないので、軽い Python の公式イメージを使う。
# ビルドと push の手順は docs/setup/06-embedding-search.md（タグ = pyproject.toml の version）。
#
# 同じリポジトリのライブラリ（apps/indexer）を使うので、ビルドの起点（コンテキスト）は apps/ にする:
#   docker buildx build -f apps/embedder/Dockerfile apps
# 除外するファイルは apps/embedder/Dockerfile.dockerignore（この Dockerfile 専用）に書いてある。
#
# モデルの重みはイメージに入れない。ジョブが起動するたびに MLflow のモデルレジストリから /models（emptyDir）にダウンロードする。

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.12.16-slim-trixie

COPY --from=uv /uv /usr/local/bin/uv

# HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE: 実行中に Hugging Face（インターネット）へ取りに行かない（重みは MLflow から取る）
ENV UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EMBEDDER_MODEL_CACHE=/models

# apps/ と同じ並びにする（embedder の pyproject.toml が ../indexer を参照するため）
WORKDIR /app/embedder

# 1. 依存パッケージ。torch だけは CPU 専用の配布元から入れる（PyPI の Linux 版は CUDA のライブラリを引き込むため）。
#    torch の依存（filelock・sympy など）は requirements に入っているので、torch 自体は --no-deps で入れる
COPY embedder/pyproject.toml embedder/uv.lock embedder/README.md ./
COPY indexer/pyproject.toml indexer/README.md ../indexer/
RUN uv export --frozen --no-dev --no-emit-project --no-emit-local --no-hashes -o /tmp/requirements.txt \
 && grep -v '^torch==' /tmp/requirements.txt > /tmp/requirements-no-torch.txt \
 && uv pip install --system -r /tmp/requirements-no-torch.txt \
 && uv pip install --system --no-deps --index-url https://download.pytorch.org/whl/cpu "torch==2.15.0+cpu" \
 && rm /tmp/requirements*.txt

# 2. 同じリポジトリのライブラリと本体（依存はもう入っているので --no-deps）
COPY indexer/src ../indexer/src
COPY embedder/src ./src
RUN uv pip install --system --no-deps ../indexer .

# root で動かさない。/models は重みのダウンロード先（ワークフローでは emptyDir をここに付ける）
RUN useradd --uid 1000 --create-home embedder && mkdir /models && chown 1000 /models
USER 1000

# DuckDB の S3 用拡張（httpfs）を、イメージ作成時に入れておく（索引を S3 から読むため。実行時にインターネットから取らない）
RUN python -c "import duckdb; duckdb.sql('INSTALL httpfs'); duckdb.sql('LOAD httpfs')"

ARG EMBEDDER_VERSION=unknown
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="mini-mlops-embedder" \
      org.opencontainers.image.version="${EMBEDDER_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.description="動画のキーフレームの画像埋め込み（SigLIP）と LanceDB でのベクトル検索"

CMD ["embedder", "--help"]
