# apps/mlflow-server — MLflow トラッキングサーバーのイメージ

## これは何か

MLflow 公式イメージ（`ghcr.io/mlflow/mlflow:v3.18.0`）に、S3 を読み書きするための `boto3`（1.44.0）だけを足したイメージです。
公式イメージには boto3 が入っていないため、そのままでは成果物（artifact）を `s3://mlflow` に保存できません。
Bitnami のチャート・イメージは配布方針が変わったので使わず、このイメージと自前の Deployment（`platform/manifests/mlflow/`）で動かします。

| 項目 | 値 |
|---|---|
| イメージ | `ghcr.io/<GHCR_USER>/mini-mlops-mlflow:3.18.0`（タグ = MLflow の版） |
| バックエンド（実験・run・レジストリの情報） | SQLite（Longhorn の PVC 1Gi）。レプリカは 1 |
| 成果物（モデル・画像） | `s3://mlflow`。サーバー経由で読み書きする（`--artifacts-destination`）ので、学習コンテナに S3 のキーを配らなくてよい |

## 実行手順

ビルドと push は [docs/setup/04-training.md](../../docs/setup/04-training.md) 手順 4。ローカルでビルドだけするなら:

```bash
docker buildx build --tag mini-mlops-mlflow:3.18.0-local --load apps/mlflow-server     # リポジトリのルートで
docker run --rm mini-mlops-mlflow:3.18.0-local python -c "import boto3, mlflow; print(mlflow.__version__, boto3.__version__)"
```

## 動作確認コマンド

```bash
kubectl -n mlflow get pod,pvc,svc
kubectl -n mlflow logs deploy/mlflow | tail -5          # Uvicorn running on http://0.0.0.0:5000
kubectl -n mlflow port-forward svc/mlflow 5050:5000     # → http://localhost:5050
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| Pod が `OOMKilled` を繰り返す（limit 1Gi） | MLflow 3.x のサーバーは既定で、裏の仕事用にジョブ実行プロセスと処理プロセス 8 つ（各約 210MB）を起動する。Deployment の環境変数 `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` で止める（止めると合計約 500MB） |
| 成果物の保存で `No module named 'boto3'` | 公式イメージをそのまま使っている。このディレクトリのイメージを使う |
| 学習 Pod から `Invalid Host header - possible DNS rebinding attack detected` | MLflow 3.5 以降のセキュリティ機能。サーバーの `--allowed-hosts` に、クラスタ内の Service 名（`mlflow.mlflow.svc` など）が入っているか確認する |
