# platform — 基盤部品（Helmfile）

## これは何か

クラスタに入れる基盤部品を Helmfile でまとめて管理します。自前のマニフェストは `manifests/`（Kustomize）に置きます。

| リリース | Namespace | チャート | 用途 | フェーズ |
|---|---|---|---|---|
| `longhorn` | `longhorn-system` | `longhorn/longhorn` 1.12.2 | 分散ブロックストレージ。default StorageClass `longhorn`（他の部品の PVC はすべてこれ） | 0 |
| `seaweedfs` | `storage` | `seaweedfs/seaweedfs` 4.52.0 | S3 互換オブジェクトストレージ（S3 API: ポート 8333） | 1 |
| `argo-workflows` | `argo` | `argo/argo-workflows` 2.0.18（v4.1.6） | ワークフローエンジン（WorkflowTemplate を実行） | 2 |
| `kuberay-operator` | `ray-system` | `kuberay/kuberay-operator` 1.7.2 | RayJob / RayCluster の operator（Ray Data の変換ジョブを動かす） | 2 |
| `kueue` | `kueue-system` | `oci://registry.k8s.io/kueue/charts/kueue` 0.20.7 | ジョブのキューイング（クォータの枠が空いたらジョブを受け入れる） | 3 |
| `kubeflow-trainer` | `kubeflow-system` | `oci://ghcr.io/kubeflow/charts/kubeflow-trainer` 2.4.0（JobSet v0.13.0 を同梱） | 分散学習の operator（TrainJob → JobSet → Pod） | 4 |

| Kustomize | 中身 | フェーズ |
|---|---|---|
| `manifests/longhorn-check/` | Longhorn の動作確認用（Namespace `longhorn-check`、テスト用 PVC 1Gi と Pod） | 0 |
| `manifests/pipelines/` | Namespace `mlops`、ServiceAccount `mcap-pipeline` と最小限の Role、S3 接続先の ConfigMap、既定リソースの LimitRange | 2 |
| `manifests/kueue/` | Kueue のキュー定義（ResourceFlavor・Cohort・ClusterQueue 2 つ・LocalQueue 2 つ・WorkloadPriorityClass 2 つ）と Namespace `training` | 3 |
| `manifests/kueue-check/` | Kueue の確認用の Job（training の枠を埋めるダミー、queue-name の無い Job）。Kustomize ではなく 1 ファイルずつ `kubectl create -f` | 3 |
| `manifests/kueue-preemption-test/` | （任意）プリエンプションを有効にした観察用のキュー。観察が終わったら消す | 3 |
| `manifests/mlflow/` | MLflow トラッキングサーバー（Namespace `mlflow`、SQLite 用の PVC 1Gi、Deployment、Service） | 4 |
| `manifests/training/` | CPU 用の学習の雛形 `torch-cpu`（ClusterTrainingRuntime）、Namespace `training` の環境変数と権限 | 4 |

### Longhorn の構成（`values/longhorn.yaml`）

- **データエンジンは V1（iSCSI）だけ**。V2（SPDK）は hugepages 2GiB と専用 CPU コアが要るので使わない。
- **StorageClass `longhorn` を default** にする。PVC のレプリカ（複製）は **2**（既定の 3 から減らした。1 ノードが落ちてもデータは残る）。
  データは各ノードの `/var/lib/longhorn` に置かれる。
- **常駐 Pod を減らす**: UI 1 つ、CSI のコントローラ（attacher / provisioner / resizer / snapshotter）を 3 つずつ → 1 つずつ。
- **リソース**: チャートで指定できるのは longhorn-manager（requests 50m / 128Mi、limits 1 CPU / 512Mi）だけ。
  UI・CSI・instance-manager など Longhorn が自分で作る Pod には resources を指定する手段が無いので、
  **LimitRange `longhorn-default-resources`**（requests 10m / 32Mi、limits 1 CPU / 512Mi）で既定値を付ける。
  limit を大きめにしているのは、instance-manager（ボリュームのデータの通り道）がメモリ不足で落ちると、そのノードのボリュームが全部止まるため。
  実際の使用量は `kubectl -n longhorn-system top pod` で確認し、予算（全体で 2GB 程度）と比べる。
- **アンインストールの安全装置**: `deletingConfirmationFlag: false` のままにしてある。`helmfile destroy` などで消そうとしても
  Longhorn の削除ジョブが失敗して止まり、データは消えない（本当に消すときは必ず事前に確認する）。
- **公開方法**: UI は ClusterIP のまま。認証が無いので `kubectl -n longhorn-system port-forward svc/longhorn-frontend 8090:80` でだけ開く。
- **ノードの準備**: 各ノードに open-iscsi（iscsid が起動している）、nfs-common などが必要。
  Longhorn 公式の `longhornctl` v1.12.2 で確認・インストールする（[00-longhorn.md](../docs/setup/00-longhorn.md) 手順 2）。

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

### Kueue の構成（`values/kueue.yaml`、`manifests/kueue/`）

- イメージ `registry.k8s.io/kueue/kueue:v0.20.7`、requests 100m / 256Mi、limit 500m / 512Mi（予算 0.5GB）。
- **基盤部品には関わらない**: `kueue.x-k8s.io/queue-name` ラベルの無いジョブは管理しない（`manageJobsWithoutQueueName: false`）。
  さらに webhook の対象 Namespace を `mlops` と `training` に限る（`managedJobsNamespaceSelector`）。
  チャートの既定のままだと、Kueue が落ちている間は他の Namespace でも Job を作れなくなるため（`failurePolicy: Fail`）。
- 使う連携: `batch/job`、`ray.io/rayjob`、`trainer.kubeflow.org/trainjob`（フェーズ 4。Trainer の CRD が入ると Kueue が自動で有効にする）。
- **waitForPodsReady**: 受け入れてから 10 分以内に全 Pod が揃わなければ、いったん戻して並び直す（3 回まで）。
- キュー: Cohort `mlops` に ClusterQueue `data-processing`（2 CPU / 5.5Gi、変換 1 本分）と `training`（2 CPU / 4.5Gi）。
  互いに空き枠を借りられる（借りる上限は相手の枠全部）。プリエンプションは使わない。優先度は `high`（1000）/ `low`（100）。
  値の決め方は [docs/architecture.md](../docs/architecture.md) の「フェーズ 3」。

### Kubeflow Trainer の構成（`values/kubeflow-trainer.yaml`、`manifests/training/`）

- イメージ `ghcr.io/kubeflow/trainer/trainer-controller-manager:v2.4.0`、同梱の JobSet `registry.k8s.io/jobset/jobset:v0.13.0`。
  どちらも requests 50m / 128Mi、limit 500m / 256Mi（チャートの既定は Trainer が resources 無し、JobSet が limit 4Gi なので指定する）。
- 同梱の学習用ランタイムは入れない（`runtimes.defaultEnabled: false`）。どれも CUDA 前提のイメージなので、CPU 用の `torch-cpu` を自分で定義する。
- `torch-cpu`: JobSet の中身は `node` という 1 種類のジョブ。Pod が 1 つでも失敗するとその Job が失敗し（`backoffLimit: 0`）、
  JobSet が全部作り直す（`failurePolicy.maxRestarts: 2`）。学習のコードがチェックポイントから再開する。
- webhook が見るのは Trainer 自身のリソース（TrainJob・ランタイム）と、JobSet のラベルが付いた Pod だけ。基盤部品の Pod には関わらない。
- Kueue の TrainJob 連携（フェーズ 3 で有効化済み）は、Trainer の CRD が入ると Kueue が自動で動かし始める。

### MLflow の構成（`manifests/mlflow/`、イメージは `apps/mlflow-server`）

- 公式イメージ（3.18.0）+ boto3。バックエンドは SQLite（Longhorn の PVC 1Gi）、レプリカ 1、ワーカー 1、更新方式 Recreate。
- 成果物は `s3://mlflow`。サーバー経由で読み書きする（`--artifacts-destination`）ので、学習の Pod に `s3://mlflow` のキーを配らない。
- `--allowed-hosts` でクラスタ内の Service 名（`mlflow.mlflow.svc:*`）と `localhost:*` だけを許可（MLflow 3.5 以降の DNS リバインディング対策）。
- 画面は ClusterIP のまま `kubectl -n mlflow port-forward svc/mlflow 5050:5000` でだけ開く（認証が無いので外に公開しない）。
- リソース: requests 100m / 512Mi、limit 1 CPU / 1Gi（予算 1GB）。MLflow 3.x が既定で起動する裏の仕事用のプロセス（9 つ、合計 2GB 超）は
  `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` で止めている（起動したままだと OOMKilled になった）。

### S3 のユーザー（`templates/seaweedfs-s3-config.json`）

| ユーザー | キー（.env） | 権限 | 使う場所 |
|---|---|---|---|
| `admin` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 全権限 | ホストからの管理・アップロード |
| `pipeline` | `PIPELINE_AWS_ACCESS_KEY_ID` / `PIPELINE_AWS_SECRET_ACCESS_KEY` | `raw` 読み取りのみ、`derived` / `curated` / `argo-artifacts` 読み書き | クラスタ内の Argo / Ray（Secret `mlops/pipeline-s3-credentials`）。変換とデータセット作成 |
| `training` | `TRAINING_AWS_ACCESS_KEY_ID` / `TRAINING_AWS_SECRET_ACCESS_KEY` | `curated` 読み書き | 学習の Pod（Secret `training/training-s3-credentials`）。データセットを読む・チェックポイントを書く |
| `mlflow` | `MLFLOW_AWS_ACCESS_KEY_ID` / `MLFLOW_AWS_SECRET_ACCESS_KEY` | `mlflow` 読み書き | MLflow サーバー（Secret `mlflow/mlflow-s3-credentials`） |

## 実行手順

手順書に沿って、フェーズ順に入れます（リポジトリのルートで実行）。

| フェーズ | 手順書 | 入れるもの |
|---|---|---|
| 0 | [docs/setup/00-longhorn.md](../docs/setup/00-longhorn.md) | `longhorn` |
| 1 | [docs/setup/01-storage.md](../docs/setup/01-storage.md) | S3 認証の Secret、`seaweedfs` |
| 2 | [docs/setup/02-pipeline.md](../docs/setup/02-pipeline.md) | `manifests/pipelines`、Secret、`argo-workflows`・`kuberay-operator` |
| 3 | [docs/setup/03-kueue.md](../docs/setup/03-kueue.md) | `kueue`、`manifests/kueue` |
| 4 | [docs/setup/04-training.md](../docs/setup/04-training.md) | `kubeflow-trainer`、`manifests/mlflow`・`manifests/training`、Secret |

Helmfile は `-l name=<リリース名>` で 1 つずつ入れられます。`-l` なしの `helmfile -f platform/helmfile.yaml apply` は全リリースを入れます
（SeaweedFS は `needs` で Longhorn の後、Longhorn は `wait` で起動を待つ）。

```bash
helmfile -f platform/helmfile.yaml diff     # 差分の確認（helm-diff プラグインが必要）
helmfile -f platform/helmfile.yaml apply
```

## 動作確認コマンド

```bash
helmfile -f platform/helmfile.yaml list                     # 6 リリースと固定したチャートの版
kubectl get storageclass                                    # longhorn (default) だけ
kubectl -n longhorn-system get pod
kubectl -n storage get pod,pvc
kubectl -n argo get pod
kubectl -n ray-system get pod
kubectl -n kueue-system get pod
kubectl -n kubeflow-system get pod
kubectl -n mlflow get pod,pvc
kubectl get clustertrainingruntime
kubectl get clusterqueue -o wide                            # data-processing / training が ACTIVE True
kubectl get localqueue -A
kubectl -n mlops get sa,role,rolebinding,configmap,secret,limitrange
```

期待される出力（抜粋）:

```
longhorn (default)   driver.longhorn.io   Delete   Immediate   true
pod/seaweedfs-all-in-one-xxxxxxxxxx-xxxxx  1/1  Running
persistentvolumeclaim/seaweedfs-all-in-one-data   Bound   5Gi    longhorn
argo-workflows-server-xxxxxxxxxx-xxxxx                1/1  Running
argo-workflows-workflow-controller-xxxxxxxxxx-xxxxx   1/1  Running
kuberay-operator-xxxxxxxxxx-xxxxx                     1/1  Running
kueue-controller-manager-xxxxxxxxxx-xxxxx             1/1  Running
jobset-controller-xxxxxxxxxx-xxxxx                    1/1  Running
kubeflow-trainer-controller-manager-xxxxxxxxxx-xxxxx  1/1  Running
mlflow-xxxxxxxxxx-xxxxx                               1/1  Running
torch-cpu
data-processing   mlops    BestEffortFIFO   0   0   True   Ready
training          mlops    BestEffortFIFO   0   0   True   Ready
serviceaccount/mcap-pipeline、role/mcap-pipeline、configmap/pipeline-s3、secret/pipeline-s3-credentials、secret/ghcr-pull、limitrange/default-resources
```

リソースの実使用量（metrics-server がある場合）: `kubectl top pod -A`

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `longhornctl check preflight` で `Service iscsid is not running` / `Package open-iscsi is not installed` | ノードの準備が足りない。`longhornctl install preflight`（[00-longhorn.md](../docs/setup/00-longhorn.md) 手順 2） |
| `longhornctl check preflight` で `multipathd.service is running` の警告 | [00-longhorn.md](../docs/setup/00-longhorn.md) 手順 3 を各ノードで行う |
| `helmfile apply` が `longhorn` で長く止まる / タイムアウト | Pod の起動待ち（`wait: true`、最大 15 分）。`kubectl -n longhorn-system get pod` で `ImagePullBackOff`（イメージ取得の失敗）や `CrashLoopBackOff` を確認。manager のログは `kubectl -n longhorn-system logs ds/longhorn-manager -c longhorn-manager` |
| longhorn-manager が `CrashLoopBackOff`、ログに `iscsiadm` / `iscsid` のエラー | open-iscsi が入っていない / iscsid が止まっている。`longhornctl check preflight` で確認 |
| PVC は `Bound` だが Pod が `ContainerCreating`、`describe` に `MountVolume.MountDevice failed ... already mounted or mount point busy` | multipathd がデバイスを掴んでいる。[00-longhorn.md](../docs/setup/00-longhorn.md) 手順 3 を行い、Pod を作り直す |
| ボリュームが `degraded` のまま | レプリカ 2 を置けるノード / ディスクの空きが足りない。`kubectl -n longhorn-system get nodes.longhorn.io` と UI の Node 画面でディスクの空きを確認（既定では空きが 25% を切ったノードには置かない） |
| `helmfile destroy` / `helm uninstall longhorn` が失敗する | 安全装置（`deletingConfirmationFlag: false`）による正常な動き。Longhorn を本当に消すときは、データを消してよいか確認してから、Longhorn 公式のアンインストール手順に従う |
| longhorn の Pod が `OOMKilled` | LimitRange の limit 512Mi を超えた。`kubectl -n longhorn-system top pod` で確認し、メモリ予算の範囲で `values/longhorn.yaml` の LimitRange か manager の limit を上げる |
| Pod が `ContainerCreating` のまま、`describe` に `secret "seaweedfs-s3-config" not found` | Secret を作る前にインストールした。[01-storage.md](../docs/setup/01-storage.md) 手順 2 を実行すれば自動で起動が進む |
| PVC が `Pending` | StorageClass `longhorn` が無い / Longhorn が不調。`kubectl get sc` を確認。別の StorageClass を使うなら values の `allInOne.data.storageClass` を変更 |
| `aws s3 ...` で `InvalidAccessKeyId` / `SignatureDoesNotMatch` | `.env` のキーと Secret の中身が食い違っている。[01-storage.md](../docs/setup/01-storage.md) 手順 2 をやり直し、`kubectl -n storage rollout restart deploy/seaweedfs-all-in-one` で再起動 |
| アップロードで `No free volumes left` / `failed to assign` | SeaweedFS はバケット（collection）ごとに volume を確保するため、上限に達した。values の `master.volumeSizeLimitMB` と `volume.dataDirs[0].maxVolumes` を調整（積が PVC サイズを超えないように）。既定の 1000MB のままだと 5Gi ではすぐ足りなくなるので 64MB にしている |
| Pod が `OOMKilled` | limit 1Gi を超えた。`kubectl -n storage top pod` で確認し、予算の範囲で limit と `GOMEMLIMIT` を上げる |
| `helmfile apply` で `Error: no matches for kind ...` などチャート取得のエラー | `helm repo update` 後に再実行。チャート版は `helmfile.yaml` で固定しているので、勝手に上がることはない |
| `argo-workflows` の apply が `crd-install` Job で止まる | 完全版 CRD をフック Job（`argo-workflows-crd-install`）で入れている。`kubectl -n argo logs job/argo-workflows-crd-install` を確認。イメージ取得に失敗しているなら時間をおいて再実行 |
| ワークフローのステップが `failed to get artifact repository` / ログ保存で `AccessDenied` | Secret `mlops/pipeline-s3-credentials` が無い、または SeaweedFS に pipeline ユーザーが登録されていない（Secret を変えたあとの再起動忘れ）。`argo-artifacts` バケットの作り忘れも確認 |
| `workflow-controller` が `OOMKilled` | 履歴のワークフローが溜まりすぎた。`argo list -n mlops` で確認し、`ttlStrategy`（7 日）より早く消すなら `argo delete -n mlops --completed` |
| `kubectl apply -k platform/manifests/kueue` が `failed calling webhook ... connection refused` | Kueue の webhook がまだ起動していない。`kubectl -n kueue-system rollout status deploy/kueue-controller-manager` を待って再実行 |
| `kubectl` のたびに `couldn't get resource list for visibility.kueue.x-k8s.io/v1beta2` の警告 / Namespace の削除が終わらない | Kueue の Pod が落ちていて、visibility API（APIService）が応答しない。Kueue を復旧させる |
| `kubeflow-trainer` の apply が webhook のエラーで止まる / `manifests/training` の apply が `failed calling webhook` | Trainer の webhook がまだ起動していない。`kubectl -n kubeflow-system rollout status deploy/kubeflow-trainer-controller-manager` を待って再実行 |
| MLflow の Pod が `OOMKilled` → `CrashLoopBackOff` | 裏の仕事用のプロセス（ジョブ実行）が起動している。Deployment に `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` があるか確認 |
| MLflow の Pod が `CrashLoopBackOff`、ログに `unable to open database file` | PVC のマウント先に書けない。`kubectl -n mlflow describe pod` で PVC が Bound か、Longhorn のボリュームが健全か確認 |
| `kueue-controller-manager` が `OOMKilled` | Workload が溜まりすぎた。終わったワークフロー（と RayJob・Workload）を `argo delete -n mlops --completed` で消し、メモリ予算の範囲で limit を上げる |
