# 変換器のコンテナイメージ。Ray クラスタの head / worker と、Argo の discover / validate ステップで共通に使う。
#
# ベースは Ray 公式の CPU 版イメージ（Python 3.12、amd64 / arm64 両対応）。
# Ray 本体・pyarrow などはベースに入っている版と uv.lock の版を揃えてあるので、入れ替わらない。
# ビルドと push の手順は docs/setup/02-pipeline.md 手順 6（タグ = pyproject.toml の version）。

# uv（Python のパッケージ管理ツール）のバイナリだけを取り出すためのステージ
FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM rayproject/ray:2.59.1-py312-cpu

COPY --from=uv /uv /usr/local/bin/uv

# Ray イメージの Python（conda 環境）。ここに直接インストールする
ENV PYTHON=/home/ray/anaconda3/bin/python \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /home/ray/converter

# 1. 依存パッケージ（コードより先に入れて、コードだけ変えたときはこの層をキャッシュから使う）
COPY --chown=ray:users pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
 && uv pip install --python "$PYTHON" -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# 2. 変換器本体（依存はもう入っているので --no-deps）
COPY --chown=ray:users src ./src
RUN uv pip install --python "$PYTHON" --no-deps .

# 3. DuckDB の S3 用拡張（httpfs）を、イメージ作成時に入れておく（実行時にインターネットから取らない）
RUN "$PYTHON" -c "import duckdb; duckdb.sql('INSTALL httpfs'); duckdb.sql('LOAD httpfs')"

ARG CONVERTER_VERSION=unknown
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="mini-mlops-converter" \
      org.opencontainers.image.version="${CONVERTER_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.description="MCAP → Parquet 変換器（Ray Data read_mcap）"

# 既定のコマンドはヘルプ表示。実際のコマンドは Argo / RayJob 側で指定する
CMD ["converter", "--help"]
