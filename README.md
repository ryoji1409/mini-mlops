# mini-mlops — CPU だけの Kubernetes で動かすミニ MLOps パイプライン

MCAP 形式のセンサーログを「取り込み → 変換 → 学習 → 検索」する MLOps パイプラインを、
CPU だけの小さな Kubernetes クラスタで組み立てて動かす検証用リポジトリです。
方針は下の「方針」、構成図は [docs/architecture.md](docs/architecture.md) を参照してください。

## 方針

- 性能やスケールは追わず、構成要素・データの流れ・運用手順を確かめることを優先する
- CPU のみ（GPU を要求するマニフェストは書かない。学習は PyTorch CPU 版 + gloo）
- バージョンはすべて固定する（`latest` は使わない。Helm チャートは `platform/helmfile.yaml`、イメージは values / マニフェスト、Python の依存は各アプリの `uv.lock`）
- すべての Pod に requests / limits を付け、下のメモリ予算に収める
- データ・PVC・バケットを消す操作は、手順書で消すものを確認してから実行する
- 秘密情報（S3 のキーなど）はコミットせず、`.env`（`.gitignore` 済み）と Kubernetes Secret で扱う
- S3 のエンドポイントや認証情報は環境変数で差し替え可能にする（S3 互換のストレージならどこでも動くように）

### メモリ予算（目安。クラスタ全体で 15GB 程度）

| コンポーネント | メモリ上限の目安 |
|---|---|
| Longhorn 一式（manager / instance-manager / CSI / UI。ノード数に比例） | 全体で 2GB 程度 |
| SeaweedFS 一式 | 1.5GB |
| Argo Workflows（controller + server） | 1GB |
| Kueue | 0.5GB |
| KubeRay operator | 0.5GB |
| Ray クラスタ（head + worker 2） | 6GB |
| Kubeflow Trainer + 学習ジョブ（worker 2） | 4GB |
| MLflow | 1GB |
| その他（Qdrant / 監視など） | 必要になったフェーズで追加 |

## 必要なツール

- `kubectl`、Helm 4 と Helmfile 1.8（`helmfile diff` を使うなら helm-diff プラグイン）
- [uv](https://docs.astral.sh/uv/)（Python 3.12 は uv が用意する）
- AWS CLI v2（S3 API の操作）
- `envsubst`（gettext。設定の雛形にキーを埋める）
- Foxglove の mcap CLI v0.4.0（MCAP の検証）
- argo CLI v4.1.6（クラスタの Argo Workflows と揃える）
- Docker（buildx。変換器イメージのビルド）

## フェーズと手順

各フェーズの手順書（何をするか・コマンド・期待される出力・よくあるエラー）は `docs/setup/` にあります。上から順に進めます。

| フェーズ | 内容 | 手順書 |
|---|---|---|
| 0 | Longhorn の導入（default StorageClass） | [docs/setup/00-longhorn.md](docs/setup/00-longhorn.md) |
| 1 | S3 互換ストレージ（SeaweedFS）の構築と、合成 MCAP の生成・検証・アップロード | [docs/setup/01-storage.md](docs/setup/01-storage.md) |
| 2 | Ray Data による MCAP 変換を Argo Workflows から実行 | [docs/setup/02-pipeline.md](docs/setup/02-pipeline.md) |
| 3 | Kueue によるジョブのキューイング（クォータ・優先度・貸し借り） | [docs/setup/03-kueue.md](docs/setup/03-kueue.md) |
| 4 | CPU で分散学習（Kubeflow Trainer v2 / Ray Train）と MLflow による実験管理 | [docs/setup/04-training.md](docs/setup/04-training.md) |
| 5 | 画像埋め込みと LanceDB での検索 | |

## ディレクトリ

| パス | 中身 |
|---|---|
| `docs/` | 手順書（`setup/`）、構成図とデータの流れ（architecture.md） |
| `platform/` | Helmfile で入れる基盤部品（Longhorn / SeaweedFS / Argo Workflows / KubeRay / Kueue / Kubeflow Trainer）、MLflow（`manifests/mlflow/`）、自前のマニフェスト（`manifests/`）、設定の雛形（`templates/`） |
| `apps/mcap-generator/` | 合成 MCAP 生成器（uv プロジェクト、ホストで実行） |
| `apps/converter/` | MCAP → Parquet 変換器（Ray Data、uv プロジェクト + Dockerfile） |
| `apps/dataset-builder/` | 学習用データセットの作成（derived + raw → curated、バージョン付き。uv プロジェクト + Dockerfile） |
| `apps/trainer/` | シーン分類の分散学習（PyTorch CPU + DDP。torchrun / Ray Train）、モデル登録、系譜の確認（uv プロジェクト + Dockerfile） |
| `apps/mlflow-server/` | MLflow トラッキングサーバーのイメージ（公式イメージ + boto3） |
| `pipelines/` | Argo WorkflowTemplate（mcap-convert: 変換、train-scene-classifier: データセット作成 → 学習 → 登録） |
| `scripts/` | 手作業では難しい処理だけのスクリプト（MCAP の検証、raw へのアップロード） |
| `output/` | 生成した MCAP の置き場（.gitignore 済み） |
