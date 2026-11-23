# platform — 基盤部品（Helmfile）

## これは何か

クラスタに入れる基盤部品を Helmfile でまとめて管理します。自前のマニフェストは `manifests/`（Kustomize）に置きます。

| リリース | Namespace | チャート | 用途 | フェーズ |
|---|---|---|---|---|
| `seaweedfs` | `storage` | `seaweedfs/seaweedfs` 4.52.0 | S3 互換オブジェクトストレージ（S3 API: ポート 8333） | 1 |
| `argo-workflows` | `argo` | `argo/argo-workflows` 2.0.18（v4.1.6） | ワークフローエンジン（WorkflowTemplate を実行） | 2 |
| `kuberay-operator` | `ray-system` | `kuberay/kuberay-operator` 1.7.2 | RayJob / RayCluster の operator（Ray Data の変換ジョブを動かす） | 2 |

| Kustomize | 中身 | フェーズ |
|---|---|---|
| `manifests/pipelines/` | Namespace `mlops`、ServiceAccount `mcap-pipeline` と最小限の Role、S3 接続先の ConfigMap、既定リソースの LimitRange | 2 |

### SeaweedFS の構成（`values/seaweedfs.yaml`）

- **allInOne モード**: master / volume / filer / S3 ゲートウェイを 1 つの Pod（`deploy/seaweedfs-all-in-one`）で動かす。単一レプリカ。
- **リソース**: requests 250m / 512Mi、limits 1 CPU / 1Gi（予算 1.5GB 以内）。`GOMEMLIMIT=900MiB` で Go のメモリ使用を抑える。
- **データ**: Longhorn の PVC 5Gi（`seaweedfs-all-in-one-data`）。
  volume 1 個の上限 64MB × 最大 64 個 = 4GiB（後述の「よくあるエラー」参照）。
- **S3 認証**: 有効。アクセスキーは Secret `storage/seaweedfs-s3-config` から読む（雛形 `templates/seaweedfs-s3-config.json` から作る。[01-storage.md](../docs/setup/01-storage.md) 手順 2）。
- **公開方法**: ClusterIP。ホストからは `kubectl -n storage port-forward svc/seaweedfs-all-in-one 8333:8333` で `localhost:8333` に転送する。
  NodePort にしたい場合は values の `allInOne.service` のコメントを参照。

### Argo Workflows の構成（`values/argo-workflows.yaml`）

- controller と server は `argo`、ワークフローの Pod は `mlops` で動く（`controller.workflowNamespaces: [mlops]`）。
- ワークフロー用の ServiceAccount / RBAC はチャートでは作らず、`manifests/pipelines` で最小権限のものを定義する
  （`workflowtaskresults` の create/patch と、`rayjobs` の create/get/list/watch/patch だけ）。
- server の認証モードは `server`（ログイン不要）。ClusterIP のまま `kubectl -n argo port-forward svc/argo-workflows-server 2746:2746` でだけ開く。
- ログとアーティファクトは `s3://argo-artifacts`（キーは `mlops` の Secret `pipeline-s3-credentials`）。`archiveLogs: true`。
- リソース: controller limit 512Mi + server limit 256Mi = 768Mi（予算 1GB）。各ステップの init / wait コンテナは 256Mi。

### KubeRay operator の構成（`values/kuberay-operator.yaml`）

- イメージ `quay.io/kuberay/operator:v1.7.2`、limit 512Mi（予算 0.5GB）。クラスタ全体の RayJob を監視する。
- Ray クラスタ（head / worker）は RayJob ごとに作られ、ジョブが終わると消える（pipelines/README.md）。

### S3 のユーザー（`templates/seaweedfs-s3-config.json`）

| ユーザー | キー（.env） | 権限 | 使う場所 |
|---|---|---|---|
| `admin` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 全権限 | ホストからの管理・アップロード |
| `pipeline` | `PIPELINE_AWS_ACCESS_KEY_ID` / `PIPELINE_AWS_SECRET_ACCESS_KEY` | `raw` 読み取りのみ、`derived` / `argo-artifacts` 読み書き | クラスタ内の Argo / Ray（Secret `mlops/pipeline-s3-credentials`） |

## 実行手順

手順書に沿って、フェーズ順に入れます（リポジトリのルートで実行）。

| フェーズ | 手順書 | 入れるもの |
|---|---|---|
| 1 | [docs/setup/01-storage.md](../docs/setup/01-storage.md) | S3 認証の Secret、`seaweedfs` |
| 2 | [docs/setup/02-pipeline.md](../docs/setup/02-pipeline.md) | `manifests/pipelines`、Secret、`argo-workflows`・`kuberay-operator` |

Helmfile は `-l name=<リリース名>` で 1 つずつ入れられます。`-l` なしの `helmfile -f platform/helmfile.yaml apply` は全リリースを入れます。

```bash
helmfile -f platform/helmfile.yaml diff     # 差分の確認（helm-diff プラグインが必要）
helmfile -f platform/helmfile.yaml apply
```

## 動作確認コマンド

```bash
helmfile -f platform/helmfile.yaml list                     # 3 リリースと固定したチャートの版
kubectl -n storage get pod,pvc
kubectl -n argo get pod
kubectl -n ray-system get pod
kubectl -n mlops get sa,role,rolebinding,configmap,secret,limitrange
```

期待される出力（抜粋）:

```
pod/seaweedfs-all-in-one-xxxxxxxxxx-xxxxx  1/1  Running
persistentvolumeclaim/seaweedfs-all-in-one-data   Bound   5Gi    longhorn
argo-workflows-server-xxxxxxxxxx-xxxxx                1/1  Running
argo-workflows-workflow-controller-xxxxxxxxxx-xxxxx   1/1  Running
kuberay-operator-xxxxxxxxxx-xxxxx                     1/1  Running
serviceaccount/mcap-pipeline、role/mcap-pipeline、configmap/pipeline-s3、secret/pipeline-s3-credentials、secret/ghcr-pull、limitrange/default-resources
```

リソースの実使用量（metrics-server がある場合）: `kubectl top pod -A`

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| Pod が `ContainerCreating` のまま、`describe` に `secret "seaweedfs-s3-config" not found` | Secret を作る前にインストールした。[01-storage.md](../docs/setup/01-storage.md) 手順 2 を実行すれば自動で起動が進む |
| PVC が `Pending` | StorageClass `longhorn` が無い / Longhorn が不調。`kubectl get sc` を確認。別の StorageClass を使うなら values の `allInOne.data.storageClass` を変更 |
| `aws s3 ...` で `InvalidAccessKeyId` / `SignatureDoesNotMatch` | `.env` のキーと Secret の中身が食い違っている。[01-storage.md](../docs/setup/01-storage.md) 手順 2 をやり直し、`kubectl -n storage rollout restart deploy/seaweedfs-all-in-one` で再起動 |
| アップロードで `No free volumes left` / `failed to assign` | SeaweedFS はバケット（collection）ごとに volume を確保するため、上限に達した。values の `master.volumeSizeLimitMB` と `volume.dataDirs[0].maxVolumes` を調整（積が PVC サイズを超えないように）。既定の 1000MB のままだと 5Gi ではすぐ足りなくなるので 64MB にしている |
| Pod が `OOMKilled` | limit 1Gi を超えた。`kubectl -n storage top pod` で確認し、予算の範囲で limit と `GOMEMLIMIT` を上げる |
| `helmfile apply` で `Error: no matches for kind ...` などチャート取得のエラー | `helm repo update` 後に再実行。チャート版は `helmfile.yaml` で固定しているので、勝手に上がることはない |
| `argo-workflows` の apply が `crd-install` Job で止まる | 完全版 CRD をフック Job（`argo-workflows-crd-install`）で入れている。`kubectl -n argo logs job/argo-workflows-crd-install` を確認。イメージ取得に失敗しているなら時間をおいて再実行 |
| ワークフローのステップが `failed to get artifact repository` / ログ保存で `AccessDenied` | Secret `mlops/pipeline-s3-credentials` が無い、または SeaweedFS に pipeline ユーザーが登録されていない（Secret を変えたあとの再起動忘れ）。`argo-artifacts` バケットの作り忘れも確認 |
| `workflow-controller` が `OOMKilled` | 履歴のワークフローが溜まりすぎた。`argo list -n mlops` で確認し、`ttlStrategy`（7 日）より早く消すなら `argo delete -n mlops --completed` |
