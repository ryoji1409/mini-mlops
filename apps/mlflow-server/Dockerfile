# MLflow のトラッキングサーバー用イメージ（フェーズ 4）。
#
# 公式イメージ（ghcr.io/mlflow/mlflow）には、S3 を読み書きするライブラリ boto3 が入っていない。
# 成果物（artifact）を s3://mlflow に置く（サーバー経由のプロキシ方式）ので、版を固定した boto3 だけを足す。
# ビルドと push の手順は docs/setup/04-training.md 手順 4。
FROM ghcr.io/mlflow/mlflow:v3.18.0

RUN pip install --no-cache-dir boto3==1.44.0

# ログを読みやすくする（UTC で出す・バッファしない）。エージェント向けの案内ログは出さない
ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    MLFLOW_DISABLE_AGENT_HINT=1

LABEL org.opencontainers.image.title="mini-mlops-mlflow" \
      org.opencontainers.image.version="3.18.0" \
      org.opencontainers.image.description="MLflow 3.18.0 + boto3（S3 互換ストレージへの artifact 保存用）"
