# 学習器のコンテナイメージ。Kubeflow Trainer（TrainJob、torchrun）と KubeRay（RayJob、Ray Train）の両方で使う。
# ワークフローの「TrainJob の完了待ち」「評価と登録」のステップもこのイメージで動く。
#
# ベースは変換器と同じ Ray 公式の CPU 版イメージ（Python 3.12、amd64 / arm64）。
# 下の層が変換器のイメージと共通なので、ノードは差分だけを取得すればよい。
# ビルドと push の手順は docs/setup/04-training.md 手順 4（タグ = pyproject.toml の version）。
#
# 0.2.0 から、同じリポジトリのライブラリ（apps/indexer・apps/navi-raster）を使うので、
# ビルドの起点（コンテキスト）は apps/ にする: docker buildx build -f apps/trainer/Dockerfile apps
# 除外するファイルは apps/trainer/Dockerfile.dockerignore（この Dockerfile 専用）に書いてある。

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM rayproject/ray:2.59.1-py312-cpu

COPY --from=uv /uv /usr/local/bin/uv

ENV PYTHON=/home/ray/anaconda3/bin/python \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    MLFLOW_DISABLE_AGENT_HINT=1

# apps/ と同じ並びにする（trainer の pyproject.toml が ../indexer・../navi-raster を参照するため）
WORKDIR /home/ray/apps/trainer

# 1. 依存パッケージ。torch だけは CPU 専用の配布元（download.pytorch.org/whl/cpu）から入れる。
#    PyPI の Linux 版 torch は CUDA のライブラリ（数 GB）を一緒に引き込むため。
#    torch の依存（filelock・sympy など）は requirements に入っているので、torch 自体は --no-deps で入れる。
#    同じリポジトリのライブラリ（--no-emit-local で除く）は、手順 2 で入れる
COPY --chown=ray:users trainer/pyproject.toml trainer/uv.lock trainer/README.md ./
COPY --chown=ray:users indexer/pyproject.toml indexer/README.md ../indexer/
COPY --chown=ray:users navi-raster/pyproject.toml navi-raster/README.md ../navi-raster/
RUN uv export --frozen --no-dev --no-emit-project --no-emit-local --no-hashes -o /tmp/requirements.txt \
 && grep -v '^torch==' /tmp/requirements.txt > /tmp/requirements-no-torch.txt \
 && uv pip install --python "$PYTHON" -r /tmp/requirements-no-torch.txt \
 && uv pip install --python "$PYTHON" --no-deps --index-url https://download.pytorch.org/whl/cpu "torch==2.15.0+cpu" \
 && rm /tmp/requirements*.txt

# 2. 同じリポジトリのライブラリと学習器本体（依存はもう入っているので --no-deps）
COPY --chown=ray:users indexer/src ../indexer/src
COPY --chown=ray:users navi-raster/src ../navi-raster/src
COPY --chown=ray:users trainer/src ./src
RUN uv pip install --python "$PYTHON" --no-deps ../indexer ../navi-raster .

ARG TRAINER_VERSION=unknown
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="mini-mlops-trainer" \
      org.opencontainers.image.version="${TRAINER_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.description="シーン分類の学習（PyTorch CPU + DDP gloo。torchrun / Ray Train）と動画ローダー"

CMD ["trainer", "--help"]
