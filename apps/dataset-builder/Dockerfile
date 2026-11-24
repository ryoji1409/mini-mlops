# データセット作成のコンテナイメージ。Argo のワークフローから batch/v1 Job（Kueue の data-processing キュー経由）として動く。
# 依存は pyarrow と Pillow だけなので、軽い Python の公式イメージを使う。
# ビルドと push の手順は docs/setup/04-training.md 手順 4（タグ = pyproject.toml の version）。

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
RUN useradd --uid 1000 --create-home builder
USER 1000

ARG BUILDER_VERSION=unknown
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="mini-mlops-dataset-builder" \
      org.opencontainers.image.version="${BUILDER_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.description="scene-cls データセットの作成（derived + raw → curated）"

CMD ["dataset-builder", "--help"]
