# 索引・タグ付け・選別のコンテナイメージ。Argo のワークフローから batch/v1 Job（Kueue の data-processing キュー経由）として動く。
# 依存は mcap・pyarrow・DuckDB だけなので、軽い Python の公式イメージを使う。
# ビルドと push の手順は docs/setup/05-video-index.md（タグ = pyproject.toml の version）。

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.12.16-slim-trixie

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
 && uv pip install --system -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY src ./src
RUN uv pip install --system --no-deps .

# root で動かさない
RUN useradd --uid 1000 --create-home indexer
USER 1000

# DuckDB の S3 用拡張（httpfs）を、イメージ作成時に入れておく（実行時にインターネットから取らない）。
# 拡張は実行ユーザーのホーム（~/.duckdb）に入るので、USER を切り替えた後で入れる
RUN python -c "import duckdb; duckdb.sql('INSTALL httpfs'); duckdb.sql('LOAD httpfs')"

ARG INDEXER_VERSION=unknown
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="mini-mlops-indexer" \
      org.opencontainers.image.version="${INDEXER_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.description="動画入り MCAP の GOP 索引・セグメントのタグ付け・DuckDB による選別"

CMD ["indexer", "--help"]
