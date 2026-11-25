# pipelines — Argo WorkflowTemplate

## これは何か

パイプラインの各工程を Argo Workflows の WorkflowTemplate として定義します。
ワークフローは Namespace `mlops` で動きます（変換の RayJob も `mlops`、学習の TrainJob / RayJob は `training`）。

| テンプレート | 内容 | フェーズ |
|---|---|---|
| `mcap-convert` | raw の MCAP を Ray Data で derived に変換し、DuckDB で検証する。RayJob は Kueue のキュー経由で動く | 2（3 で Kueue 経由に） |
| `train-scene-classifier` | データセットを作り（curated）、シーン分類を分散学習し（TrainJob か RayJob）、閾値を超えたら MLflow に登録する | 4 |
| `build-video-index` | 動画入り MCAP の GOP 索引（summary だけを Range GET）と、セグメントのタグ付け | 5 |
| `select-and-curate` | 選別の SQL でセグメントを選び、サンプル一覧（manifest）とシャードを curated に作る | 5 |

### train-scene-classifier の流れ

```
build-dataset ──▶ train（engine で選ぶ）──────────────────────────▶ register
 batch/v1 Job       trainjob: TrainJob を作る → wait-trainjob で完了を待つ   trainer register
 data-processing    rayjob  : RayJob を作り、状態を見張る                   正解率 ≥ 閾値なら
 キュー経由          （どちらも Namespace training、training キュー経由）     scene-classifier@candidate
```

| パラメータ | 既定 | 意味 |
|---|---|---|
| `dataset-version` | `1` | データセットのバージョン（`s3://curated/scene-cls/v<N>`）。同じ中身なら作り直さない、違う中身ならエラー |
| `engine` | `trainjob` | `trainjob`（Kubeflow Trainer + torchrun）/ `rayjob`（KubeRay + Ray Train） |
| `workers` | `2` | 学習ワーカーの数（world_size）。training キューの枠に合わせて 1 か 2 |
| `epochs` / `batch-size` / `lr` / `seed` | `5` / `64` / `0.001` / `42` | 学習のパラメータ（MLflow に記録される） |
| `threshold` | `0.8` | 登録の閾値（検証の正解率。時間帯・天候の低いほう） |
| `resume-from` | 空 | 前の学習（ワークフロー名）のチェックポイントから続ける |
| `priority` | `low` | Kueue の優先度 |
| `trainer-image` / `dataset-builder-image` | `ghcr.io/OWNER/…:0.1.0` | 登録するときに OWNER を置き換える |

- **ジョブ名 = ワークフロー名**: TrainJob / RayJob の名前をワークフロー名にして、チェックポイントの置き場所（`s3://curated/checkpoints/<名前>/`）と
  MLflow の run の目印（タグ `mini-mlops.job_name`）に使う。register はこのタグで run を探す。
- **TrainJob の完了待ち**: Argo の resource テンプレートの成功条件は `status.xxx == 値` の形しか書けず、TrainJob の `status.conditions`
  （リストの中の `type: Complete`）を判定できない。そこで `trainer wait-trainjob` が Kubernetes の API で conditions を見張る。
  JobSet が作り直している間・Kueue で待っている間は待ち続ける。
- **データセット作成も Kueue を通す**: Argo のステップの Pod は Kueue を通らないので、batch/v1 Job の形にして data-processing キューに入れる。
  同じバージョンを 2 つのワークフローが同時に作らないよう、mutex（`scene-dataset-v<N>`）を付けている。
- **片付け**: TrainJob / RayJob は別の Namespace（training）なので ownerReference を付けられず、ワークフローを消しても残る（04-training.md の片付け）。

### build-video-index / select-and-curate の流れ（フェーズ 5）

```
build-video-index:  build-index ──▶ tag            select-and-curate:  select
                    batch/v1 Job     batch/v1 Job                       batch/v1 Job
                    （どれも data-processing キュー経由、SA curate-pipeline、イメージ mini-mlops-indexer）
```

| パラメータ | 既定 | 意味 |
|---|---|---|
| `force`（build-video-index） | `false` | `true` なら作成済みの索引も作り直す |
| `verify-gops`（build-video-index） | `1` | 参照位置の確認のために、セッションごとに実際に読む GOP の数 |
| `dataset` / `version`（select-and-curate） | `night-rain` / `1` | 出力先 `s3://curated/<dataset>/v<version>/`。同じ中身ならスキップ、違う中身ならエラー |
| `sql` | `night_rain` | 選別の SQL（`apps/indexer/src/indexer/sql/<名前>.sql`。イメージに入っているもの） |
| `frames` / `seed` | `first` / `0` | GOP のどのフレームをサンプルにするか（`first` / `random` / `all`） |
| `gops-per-shard` | `50` | シャード 1 つあたりの GOP の数 |
| `segment-ids-uri` | 空 | 外から渡す segment_id の一覧（Parquet）。SQL から `candidates` という表で使える |

- 索引は同時に 2 つ作らない（mutex `video-index`）。選別は同じデータセット・バージョンで mutex
- 手順は [docs/setup/05-video-index.md](../docs/setup/05-video-index.md)

### mcap-convert の流れ

```
discover ──(未処理 > 0)──▶ convert（RayJob を作って待つ）──▶ validate
   │                          │                                  │
   │ converter discover       │ suspend: true で作る → Kueue が    │ converter validate
   │ 未処理の件数を出力        │ 受け入れたら Ray head 1 + worker 2 │ DuckDB で行数照合
   │                          │ entrypoint: converter convert     │
   └─(未処理 = 0)── convert をスキップ ───────────────────────────▶┘
```

3 つのステップとも `--device-id` で対象のデバイスを絞れます（空なら全デバイス）。

| パラメータ | 既定 | 意味 |
|---|---|---|
| `converter-image` | `ghcr.io/OWNER/mini-mlops-converter:<version>`（登録時に OWNER を置き換える。タグは pyproject.toml の version と揃える） | 変換器のイメージ |
| `force` | `false` | `true` で変換済み・隔離済みのセッションも作り直す |
| `fail-on-quarantine` | `false` | `true` で隔離があればワークフローを失敗にする |
| `device-id` | 空（全デバイス） | 対象のデバイス（例 `dev-011`）。デバイスごとに別のワークフローを並行して投げられる |
| `priority` | `low` | Kueue の優先度（`low` / `high`）。待ち行列では high が先に受け入れられる |
| `queue-name` | `data-processing` | RayJob を入れる Kueue の LocalQueue（プリエンプションの観察では `preemption-test`） |

- **RayJob の成否**: `successCondition: status.jobStatus == SUCCEEDED`、`failureCondition: status.jobDeploymentStatus in (Failed, ValidationFailed)`
  （ジョブの失敗・期限切れ・submitter の失敗はどれも `jobDeploymentStatus` が `Failed` になる）。
  Kueue で待っている間は `Suspended` で、どちらにも当たらないので、ワークフローは失敗せずに待ち続ける。
- **Kueue**: RayJob に `kueue.x-k8s.io/queue-name`（既定 `data-processing`）と `kueue.x-k8s.io/priority-class`（既定 `low`）を付け、
  `suspend: true` で作る。Kueue がクォータの空きを確かめて受け入れると suspend が外れ、Ray クラスタが立ち上がる。
  枠が足りない間は Ray の Pod を 1 つも作らない（全部か無しか）。キューの設計は docs/architecture.md の「フェーズ 3」。
- **制限時間**: ワークフロー全体は 6 時間（Kueue での待ち + 実行）、RayJob は 30 分（実行だけ。KubeRay は受け入れ時に開始時刻を付け直す）。
- **後片付け**: `shutdownAfterJobFinishes: true` + `ttlSecondsAfterFinished: 0` で、ジョブが終わったらすぐ Ray クラスタを消す。
  Kueue はジョブ終了と同時に枠を返すので、Ray クラスタを残すと次に受け入れたジョブの Pod がノードに載らなくなる（フェーズ 3 で実際に起きた）。
  RayJob 自体は ownerReference でワークフローに紐づき、ワークフローが消える（7 日後）と一緒に消える。
- **同時実行**: mutex 名を `mcap-convert-<device-id>` にして、**同じデバイスのワークフローは 1 つずつ**（同じセッションの取り合いを防ぐ）。
  別のデバイスは並んで動き、RayJob の順番は Kueue が決める。device-id を空にした全デバイスの実行は、デバイス指定の実行と同時に投げない。
- **ログ**: 各ステップのログは `s3://argo-artifacts/mlops/<ワークフロー名>/<Pod 名>/` に保存（Pod が消えても UI から見られる）。
  RayJob のジョブログは submitter Pod（`kubectl -n mlops logs job/<RayJob 名>`）で見る。

### リソース（limits）

| 対象 | limit | 備考 |
|---|---|---|
| Ray head | 1 CPU / 3Gi | `num-cpus=0`（タスクは載せない）、object store 200MB。Ray 2.59 の head は Dashboard の子プロセスなどで約 2.3GB 使う |
| Ray worker × 2 | 1 CPU / 1.25Gi | object store 250MB。変換時の実測ピークは約 0.6GB |
| submitter Pod | 0.5 CPU / 512Mi | `ray job submit` してログを追うだけ |
| **Ray 合計** | **6Gi** | 予算 6GB（Kueue は requests の合計 1.6 CPU / 4.75Gi で枠を数える） |
| discover / validate | 1 CPU / 512Mi | 短時間だけ動く。init / wait コンテナは別に 256Mi（Argo の executor 設定） |

## 実行手順

登録・実行・Ray Dashboard・隔離の確認の手順は [docs/setup/02-pipeline.md](../docs/setup/02-pipeline.md)（手順 7〜10）、
Kueue でのキューイング・優先度・貸し借りの確認は [docs/setup/03-kueue.md](../docs/setup/03-kueue.md) にあります。
**フェーズ 3 以降のテンプレートは Kueue が前提**です（Kueue が無いと RayJob が suspend のまま動かない）。
train-scene-classifier の手順は [docs/setup/04-training.md](../docs/setup/04-training.md) にあります。
よく使うコマンドだけ抜き出すと:

```bash
# 登録（OWNER を GitHub のユーザー名に置き換える。.env を読み込んだシェルで）
sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" pipelines/mcap-convert.yaml | kubectl apply -f -

# 実行して完了まで見守る
argo submit -n mlops --from workflowtemplate/mcap-convert --watch
argo submit -n mlops --from workflowtemplate/mcap-convert -p force=true --watch   # 作り直し
argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=dev-011 -p priority=high   # デバイスを絞り、優先度 high で

# 学習（フェーズ 4）
sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" pipelines/train-scene-classifier.yaml | kubectl apply -f -
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=trainjob -p workers=2 --watch
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=rayjob --watch

# 動画の索引・選別（フェーズ 5）
for f in build-video-index select-and-curate; do sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" "pipelines/$f.yaml" | kubectl apply -f -; done
argo submit -n mlops --from workflowtemplate/build-video-index --watch
argo submit -n mlops --from workflowtemplate/select-and-curate -p dataset=night-rain -p version=1 -p sql=night_rain --watch
```

## 動作確認コマンド

```bash
argo get -n mlops @latest
argo logs -n mlops @latest | grep -E "変換対象|OK |QUARANTINED|MISMATCH|MISSING|検証"
kubectl -n mlops get workflow,rayjob,raycluster,pod
kubectl -n mlops get workloads                      # Kueue の Workload（RayJob 1 つにつき 1 つ。ADMITTED が True なら受け入れ済み）
kubectl get clusterqueue -o wide                    # 待っている数（PENDING）と受け入れ済みの数（ADMITTED）
kubectl -n mlops logs job/<RayJob 名> -f            # ジョブのログ（RayJob 名は argo get の convert の出力 rayjob-name）
```

期待される出力（初回の validate ステップのログ、抜粋）:

```
OK          dev-001-20261104T013238Z         imu=3000, gnss=300, frames_front=150
...
検証結果: {"sessions": 6, "ok": 6, "quarantined": 0, "mismatch": 0, "missing": 0, "orphans": 0, "invalid_metadata": 0, "unreadable_datasets": 0}
検証 OK
```

2 回目は discover が `変換対象 0` になり、convert はスキップされます（Ray クラスタを立てない）。

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| Pod が `ErrImagePull` / `ImagePullBackOff`、`describe` に `401 Unauthorized` | GHCR の pull 用トークンが無い / 権限不足。`GHCR_PULL_TOKEN`（read:packages）を確認して Secret `ghcr-pull` を作り直す（[02-pipeline.md](../docs/setup/02-pipeline.md) 手順 4） |
| `ImagePullBackOff` で `no match for platform in manifest` | イメージの CPU アーキテクチャがノードと違う。`.env` の `CONVERTER_PLATFORM` をノードに合わせて（例: `linux/arm64`）ビルドし直す |
| ワークフローが `Pending` のまま、`waiting for lock` | 同じデバイスの mcap-convert が動いている（デバイスごとの mutex）。`argo list -n mlops` で確認 |
| convert が終わらず、RayJob が `Suspended` のまま | Kueue で順番待ち。`kubectl -n mlops describe workloads` の Message で理由を確認（[03-kueue.md](../docs/setup/03-kueue.md) のよくあるエラー）。Kueue が入っていない場合は永久に待つ |
| discover が `AccessDenied` / `InvalidAccessKeyId` | Secret `pipeline-s3-credentials` のキーが SeaweedFS に登録されていない。[01-storage.md](../docs/setup/01-storage.md) 手順 2 をやり直して SeaweedFS を再起動 |
| convert が `forbidden: User "system:serviceaccount:mlops:mcap-pipeline" cannot create resource "rayjobs"` | Role が無い。`kubectl apply -k platform/manifests/pipelines` |
| convert が `cannot set blockOwnerDeletion if an ownerReference refers to a resource you can't set finalizers on` | API サーバで OwnerReferencesPermissionEnforcement が有効な環境。Role に `argoproj.io` の `workflows/finalizers` への `update` を足す |
| RayJob の head Pod が `OOMKilled` | head の limit（3Gi）を超えた。ドライバのメモリが増えた可能性。`kubectl -n mlops describe pod <head>` で確認し、メモリ予算の範囲で limit を上げる |
| RayJob が `Pending` のまま（head / worker が `Pending`） | ノードのメモリが足りず配置できない（head は request 2.5Gi）。`kubectl describe pod` の Events を確認し、他の負荷を減らす |
| validate が `ORPHAN` / `MISMATCH` で失敗 | apps/converter/README.md の「よくあるエラー」を参照。`argo submit ... -p force=true` で作り直せる |
| `argo logs` でログが出ない | Pod が GC 済み（成功 10 分後）。UI ではアーティファクト（s3://argo-artifacts）に保存されたログが見られる |
| train-scene-classifier の学習で失敗・止まる | [docs/setup/04-training.md](../docs/setup/04-training.md) の「よくあるエラー」を参照 |
| wait-trainjob が `403 Forbidden` | ServiceAccount `mlops/train-pipeline` に training の TrainJob を get する権限が無い。`kubectl kustomize platform/manifests/training` を適用し直す |
