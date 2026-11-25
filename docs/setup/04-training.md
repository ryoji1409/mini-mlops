# フェーズ 4: CPU での分散学習と MLflow による実験管理

変換済みのフレームから学習用のデータセットを作り、小さな CNN を 2 台のワーカーで分散学習（PyTorch DDP、通信は gloo）して、
結果を MLflow に記録・登録します。学習の起動方法は 2 通り用意し、使い分けられるようにしています。

| 方式（`engine`） | 起動 | 向いている場面 |
|---|---|---|
| `trainjob` | Kubeflow Trainer の TrainJob → torchrun | PyTorch のコードをそのまま Kubernetes で動かす。Pod はワーカーだけで軽い |
| `rayjob` | KubeRay の RayJob → Ray Train（TorchTrainer） | 変換（Ray Data）からの流れで、読み込み・前処理と学習を Ray でまとめる。head の分だけ重い |

設計の理由は [docs/architecture.md](../architecture.md) の「フェーズ 4」、ワークフローは [pipelines/README.md](../../pipelines/README.md)、
各アプリは [apps/dataset-builder](../../apps/dataset-builder/README.md)・[apps/trainer](../../apps/trainer/README.md)・[apps/mlflow-server](../../apps/mlflow-server/README.md)。

**完了条件**:

1. ワーカー 2 台で学習が完走し、両方の Pod のログで world_size=2、rank 0 / 1 が確認できる（手順 9）
2. MLflow UI で run のパラメータ・メトリクス・混同行列・モデルが見え、レジストリに `scene-classifier` が `candidate` 付きで登録されている（手順 10）
3. 登録されたモデルから、データセットのマニフェスト → derived の _manifests → raw の MCAP までたどれる（手順 11）
4. 学習中にワーカー Pod を 1 つ消したときの動きを観察し、チェックポイントから再開して最後まで終えられる（手順 12）
5. TrainJob が training キューの Workload として受け入れられる。変換と同時に投げたときの貸し借りを記録する（手順 9・13）
6. ワーカー 1 台と 2 台で、1 エポックあたりの時間を比べる（手順 14）

## 前提

- フェーズ 3（[03-kueue.md](03-kueue.md)）が終わっている（Kueue の training キューがある）
- `.env` を読み込み、S3 の port-forward が動いている（[01-storage.md](01-storage.md) 手順 4）
- 手元に `uv` がある（系譜の確認で `apps/trainer` をホストから動かす）

```bash
set -a; source .env; set +a
```

## 1. 学習用のデータを追加する

今の 26 セッションでは、検証用に（時間帯, 天候）の 6 通りの組を全部入れるには足りないので、
6 デバイス × 10 セッション（dev-021〜dev-026）を追加します（既存の ID とは重ならない）。

```bash
(cd apps/mcap-generator && uv run mcap-generate --first-device 21 --devices 6 --sessions-per-device 10 --out ../../output --seed 21)
scripts/validate-mcap.sh output                 # 最後に OK=86 NG=0（既存 26 + 追加 60）
scripts/upload-raw.sh output                    # 既存の 26 本は「スキップ」、追加の 60 本がアップロードされる
```

全デバイスを変換します（変換器 0.2.1 より前の版で変換したセッションも、この実行で 0.2.1 で作り直される）。

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert --watch      # 86 本の変換で 10〜20 分
argo logs -n mlops @latest | grep -E "変換対象|検証結果"
# 検証結果: {"sessions": 87, "ok": 86, "quarantined": 1, ...}   ← dev-901（壊れた MCAP）が残っていれば隔離 1
```

## 2. S3 のユーザー（training・mlflow）を登録する

`.env.example` に増えた `TRAINING_AWS_*` と `MLFLOW_AWS_*` の 4 行を `.env` に書き足し、キーを作って埋めます
（作り方は [01-storage.md](01-storage.md) 手順 1）。

| ユーザー | 権限 | 使う場所 |
|---|---|---|
| training | `curated` の読み書き | 学習の Pod（データセットを読む・チェックポイントを書く） |
| mlflow | `mlflow` の読み書き | MLflow サーバー（成果物の保存）。学習の Pod はこのキーを持たない |
| pipeline（変更） | `curated` の読み書きを追加 | データセット作成の Job |

[01-storage.md](01-storage.md) 手順 2 をもう一度実行して Secret を更新し、SeaweedFS を再起動します（Secret の変更は再起動しないと反映されない）。

```bash
kubectl -n storage rollout restart deploy/seaweedfs-all-in-one
kubectl -n storage rollout status deploy/seaweedfs-all-in-one --timeout=5m
aws s3 ls                                       # curated と mlflow があること（フェーズ 1 の手順 5 で作ってある）
```

## 3. Kubeflow Trainer（と JobSet）を入れる

```bash
helmfile -f platform/helmfile.yaml -l name=kubeflow-trainer diff
helmfile -f platform/helmfile.yaml -l name=kubeflow-trainer apply      # webhook が動き出すまで待つ
kubectl -n kubeflow-system get pod
kubectl get crd | grep -E "trainer.kubeflow.org|jobset"
```

期待される出力:

```
NAME                                                  READY   STATUS
jobset-controller-xxxxxxxxxx-xxxxx                    1/1     Running
kubeflow-trainer-controller-manager-xxxxxxxxxx-xxxxx  1/1     Running

clustertrainingruntimes.trainer.kubeflow.org / trainingruntimes.trainer.kubeflow.org / trainjobs.trainer.kubeflow.org / jobsets.jobset.x-k8s.io
```

Kueue が TrainJob の連携を有効にしたことを確かめます（Kueue は TrainJob の CRD が入るのを待っていて、入ると自動で有効にする）。

```bash
kubectl -n kueue-system logs deploy/kueue-controller-manager | grep -i "trainjob" | tail -3
# … "API now available, starting controller" … gvk=trainer.kubeflow.org/v1alpha1, Kind=TrainJob
```

## 4. イメージをビルドして push する

3 つのイメージを作ります（ビルドの仕方は [02-pipeline.md](02-pipeline.md) 手順 6 と同じ。タグは各 `pyproject.toml` の version）。

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
PLATFORM="${CONVERTER_PLATFORM:-linux/amd64,linux/arm64}"

# データセット作成
V=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/dataset-builder/pyproject.toml)
docker buildx build --platform "$PLATFORM" --build-arg BUILDER_VERSION="$V" --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "ghcr.io/${GHCR_USER}/mini-mlops-dataset-builder:${V}" --push apps/dataset-builder

# 学習器（Ray のイメージがベースなので、変換器と同じく大きい。下の層は変換器と共通）。
# 0.2.0 からは同じリポジトリのライブラリ（apps/indexer・apps/navi-raster）も入れるので、ビルドの起点は apps/（-f で Dockerfile を指定）
V=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/trainer/pyproject.toml)
docker buildx build --platform "$PLATFORM" --build-arg TRAINER_VERSION="$V" --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "ghcr.io/${GHCR_USER}/mini-mlops-trainer:${V}" --push -f apps/trainer/Dockerfile apps

# MLflow サーバー（公式イメージ + boto3。タグは MLflow の版）
docker buildx build --platform "$PLATFORM" --tag "ghcr.io/${GHCR_USER}/mini-mlops-mlflow:3.18.0" --push apps/mlflow-server
```

初めて push したパッケージは非公開で作られます。GitHub の Packages 画面で Private になっていることを確認します。

## 5. Namespace・権限・Secret を作り、MLflow を立てる

Secret はシェルに読み込んだ `.env` の値から作ります。キーが空のまま作ると、学習の Pod が S3 で `ACCESS_DENIED` になるので、先に確かめます。

```bash
set -a; source .env; set +a
for v in TRAINING_AWS_ACCESS_KEY_ID TRAINING_AWS_SECRET_ACCESS_KEY MLFLOW_AWS_ACCESS_KEY_ID MLFLOW_AWS_SECRET_ACCESS_KEY GHCR_USER GHCR_PULL_TOKEN; do
  [ -n "$(eval "printf %s \"\$$v\"")" ] || echo "空です: $v"
done                                                        # 何も出なければ OK（「空です」が出たら .env を埋めて読み込み直す）
```

```bash
# mlops: ワークフローの ServiceAccount（train-pipeline）と、データセットの出力先（CURATED_URI）
kubectl apply -k platform/manifests/pipelines

# mlflow: Namespace、PVC、Deployment、Service（OWNER を置き換える）
kubectl kustomize platform/manifests/mlflow | sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" | kubectl apply -f -
kubectl -n mlflow create secret generic mlflow-s3-credentials \
  --from-literal=AWS_ACCESS_KEY_ID="$MLFLOW_AWS_ACCESS_KEY_ID" --from-literal=AWS_SECRET_ACCESS_KEY="$MLFLOW_AWS_SECRET_ACCESS_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

# training: CPU 用の学習の雛形（torch-cpu）、環境変数、権限（OWNER を置き換える）
kubectl kustomize platform/manifests/training | sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" | kubectl apply -f -
kubectl -n training create secret generic training-s3-credentials \
  --from-literal=AWS_ACCESS_KEY_ID="$TRAINING_AWS_ACCESS_KEY_ID" --from-literal=AWS_SECRET_ACCESS_KEY="$TRAINING_AWS_SECRET_ACCESS_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

# GHCR の pull 用（mlflow と training の両方に）
for ns in mlflow training; do
  kubectl -n "$ns" create secret docker-registry ghcr-pull \
    --docker-server=ghcr.io --docker-username="$GHCR_USER" --docker-password="$GHCR_PULL_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
done

kubectl -n mlflow rollout status deploy/mlflow --timeout=5m
kubectl get clustertrainingruntime torch-cpu
```

## 6. MLflow の画面を開く

```bash
kubectl -n mlflow port-forward svc/mlflow 5050:5000     # → http://localhost:5050（Ctrl-C で終了）
curl -s http://localhost:5050/health; echo              # OK と出れば通っている
```

画面が出れば OK です（まだ run はない）。手元の 5000 番は、OS の機能やほかのアプリが使っていることがあるので、5050 番で開きます。

## 7. WorkflowTemplate を登録する

```bash
sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" pipelines/train-scene-classifier.yaml | kubectl apply -f -
argo template get -n mlops train-scene-classifier | grep -A16 Parameters
```

## 8. 監視の画面を用意する

別のターミナルで、キュー・Workload・学習の Pod を 5 秒ごとに表示しておきます（Ctrl-C で終了）。

```bash
while true; do clear; date
  kubectl get clusterqueue -o wide
  kubectl get workloads -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,QUEUE:.spec.queueName,ADMITTED:.status.conditions[?(@.type=="Admitted")].status,FINISHED:.status.conditions[?(@.type=="Finished")].status' | tail -8
  kubectl -n training get trainjob,rayjob 2>/dev/null | tail -6
  kubectl -n training get pod -o wide --no-headers 2>/dev/null | awk '{print $1, $3, $7}'
  sleep 5; done
```

## 9. TrainJob（ワーカー 2 台）で学習する（完了条件 1・5）

```bash
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=trainjob -p workers=2 --watch
```

監視の画面では次の順に変わります。

| 段階 | 見えるもの |
|---|---|
| データセット作成 | mlops の Workload `job-<ワークフロー名>-dataset-…`（data-processing キュー）が受け入れられる |
| 学習の受け入れ待ち | TrainJob ができ、training の Workload `trainjob-<名前>-…` が作られる。受け入れまで Pod は無い |
| 学習中 | Pod `<名前>-node-0-0-xxxxx` と `<名前>-node-0-1-xxxxx` が Running（JobSet → Job → Pod） |
| 登録 | register ステップが動き、ワークフローが Succeeded |

**両方のワーカーのログで world_size と rank を確かめます**（完了条件 1）。

```bash
WF=$(argo list -n mlops -o name | grep train-scene | head -1); echo "$WF"      # いちばん新しい学習のワークフロー
kubectl -n training logs -l jobset.sigs.k8s.io/jobset-name="$WF" --prefix --tail=-1 | grep -E "world_size=|epoch=|チェックポイントを保存"
```

期待される出力（抜粋）:

```
[pod/<名前>-node-0-0-xxxxx/node] … rank=0 world_size=2 host=<名前>-node-0-0-xxxxx threads=1 engine=torchrun
[pod/<名前>-node-0-1-xxxxx/node] … rank=1 world_size=2 host=<名前>-node-0-1-xxxxx threads=1 engine=torchrun
[pod/<名前>-node-0-0-xxxxx/node] … rank=0 epoch=1/5 {'train_loss': …, 'epoch_train_seconds': …, 'val_time_of_day_accuracy': …, 'val_weather_accuracy': …}
[pod/<名前>-node-0-0-xxxxx/node] … チェックポイントを保存しました: s3://curated/checkpoints/<名前>/epoch-0001.pt
```

（Pod は成功してもしばらく残る。消えていたら MLflow の画面で確認する）

**TrainJob が training キューの Workload として受け入れられたことを確かめます**（完了条件 5）。

```bash
kubectl -n training get workloads
kubectl -n training describe "$(kubectl -n training get workloads -o name | grep "$WF")" | grep -E "Queue Name|Cluster Queue|Name: |Count|Cpu|Memory|Type|Reason" | head -30
```

期待される出力（抜粋）:

```
NAME                                QUEUE      RESERVED IN   ADMITTED   FINISHED
trainjob-<名前>-xxxxx               training   training      True       True
  Pod Sets:  Count: 2  Name: node        ← ワーカー 2 台を 1 まとまりとして受け入れた（全部か無しか）
  Resource Usage:  Cpu: 1  Memory: 3Gi    ← requests の合計（ワーカー 1 台 500m / 1536Mi）
```

ワークフローの最後（register）のログ:

```bash
argo logs -n mlops "$WF" | grep -E "登録しました|登録しません|registered"
# 登録しました: scene-classifier version 1（エイリアス candidate）正解率 0.9xxx ≥ 閾値 0.8
```

## 10. MLflow の画面で確認する（完了条件 2）

http://localhost:5050 （手順 6 の port-forward）で:

- **Experiments → scene-classifier → run（名前 = ワークフロー名）**
  - Parameters: `lr`、`batch_size`、`epochs`、`seed`、`world_size`=2、`engine`=torchrun
  - Metrics: `train_loss`、`val_loss`、`val_time_of_day_accuracy`、`val_weather_accuracy`、`epoch_train_seconds`（エポックごとのグラフ）
  - Artifacts: `confusion_matrix.png`（行 = 正解、列 = 予測）
  - Tags: `dataset.version`、`dataset.hash`、`dataset.manifest`、`trainer.image`、`converter.versions` など（系譜）
  - Logged models: `model`
- **Models → scene-classifier**: version 1 に Aliases `@candidate`

画像の表示で一瞬エラーが出ることがあります。MLflow の画面は、まず S3 から直接画像を取ろうとし（クラスタ内のアドレスなのでブラウザからは届かない）、
失敗するとサーバー経由で取り直す仕組みのためです。

## 11. 系譜をたどる（完了条件 3）

登録されたモデルから、学習の run → データセットのマニフェスト → derived の _manifests → raw の MCAP の順にたどり、
各段で記録されたハッシュと実際のファイルを照合します。ホストから実行するので、**port-forward が 2 つ** 必要です
（MLflow: 手順 6 の 5050、S3: [01-storage.md](01-storage.md) 手順 4 の 8333。SeaweedFS を再起動すると 8333 は切れるので張り直す）。
S3 のキーは `.env` の admin のものを使います。

```bash
(cd apps/trainer && uv sync && MLFLOW_TRACKING_URI=http://localhost:5050 uv run trainer trace --model-name scene-classifier --alias candidate --limit 3)
```

期待される出力:

```
モデル        scene-classifier version 1（エイリアス candidate）
  └ run       <run_id>（ジョブ <名前>、方式 torchrun、イメージ ghcr.io/<user>/mini-mlops-trainer:0.1.1）
    └ データセット s3://curated/scene-cls/v2/manifest.json
        dataset_hash 1a2b3c…（run のタグと一致）、学習 16xxx / 検証 2xxx フレーム、変換器 0.2.1
        └ train s3://derived/_manifests/dev-001-20261104T013238Z.json（sha256 一致、変換器 0.2.1）
            └ raw s3://raw/device=dev-001/date=2026-11-04/session_id=dev-001-20261104T013238Z/dev-001-20261104T013238Z.mcap（sha256 817c79…）
        …（ほか 83 セッション。--limit 0 で全部）
```

終了コードが 0 なら、すべての段でハッシュが一致しています（1 なら「不一致!」の行を確認）。

## 12. 学習中にワーカーを 1 つ消して、再開を確かめる（完了条件 4）

### 12-a. TrainJob（JobSet の作り直し）

エポック数を増やして投げ、2 エポック目のチェックポイントが保存されたら、rank 1 の Pod を消します。

```bash
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=trainjob -p workers=2 -p epochs=10
WF=$(argo list -n mlops -o name | grep train-scene | head -1); echo "$WF"
kubectl -n training logs -f -l jobset.sigs.k8s.io/jobset-name="$WF" --prefix | grep -E "epoch=|チェックポイント"   # epoch=2/10 が出たら Ctrl-C

kubectl -n training get pod -l jobset.sigs.k8s.io/jobset-name="$WF"           # 消す Pod を確認
kubectl -n training delete "$(kubectl -n training get pod -l jobset.sigs.k8s.io/jobset-name="$WF" -o name | grep node-0-1)"     # -o name は pod/<名前> の形なので、delete の後に pod と書かない
```

観察すること:

```bash
kubectl -n training get jobset "$WF" -o jsonpath='{.status.restarts}{"\n"}'             # 作り直した回数（0 → 1）
kubectl -n training get pod -l jobset.sigs.k8s.io/jobset-name="$WF" -w                  # 2 台とも新しい Pod に入れ替わる
kubectl -n training logs -l jobset.sigs.k8s.io/jobset-name="$WF" --prefix | grep -E "再開|world_size="
# … チェックポイントから再開します: s3://curated/checkpoints/<名前>/epoch-0002.pt（2 エポック終了済み）
kubectl -n training get events --sort-by=.lastTimestamp | grep -i -E "jobset|restart|fail" | tail
```

予想される動き（設定から）:

1. 消した Pod の Job が失敗する（`backoffLimit: 0`）。残った rank 0 も、通信相手が消えて gloo のエラーで止まる
2. JobSet が Job を全部作り直す（`failurePolicy.maxRestarts: 2`、`status.restarts` が 1 になる）。Kueue の Workload は受け入れたまま
3. 新しい 2 台が、最後のチェックポイント（epoch-0002）から学習を続ける。MLflow には同じ run に記録が続く
4. ワークフローは失敗せず、最後まで終わって登録まで進む

作り直しの上限（2 回）を超えて TrainJob が `Failed` になった場合は、そのワークフロー名を指定して、チェックポイントから続きを実行します。

```bash
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=trainjob -p epochs=10 -p resume-from="$WF" --watch
# 学習のログに「チェックポイントから再開します: …/<前の名前>/epoch-000N.pt」。MLflow は前と同じ run に続けて記録される
```

### 12-b. RayJob（Ray Train の自動再開）

```bash
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=rayjob -p workers=2 -p epochs=10
WF=$(argo list -n mlops -o name | grep train-scene | head -1); echo "$WF"
kubectl -n training get pod -l ray.io/cluster -o wide                                    # head 1 + worker 2
kubectl -n training logs -f "job/$WF" | grep -E "epoch=|world_size="                    # epoch=2/10 が出たら Ctrl-C（submitter のログ）
# submitter の Job は Ray の head が Ready になってから作られる。それまでは NotFound になるので、少し待ってやり直す
# rank=0 と rank=1 の host（と node）が別々の worker の Pod になっていること
kubectl -n training delete "$(kubectl -n training get pod -l ray.io/node-type=worker -o name | head -1)"
```

観察すること:

```bash
kubectl -n training get pod -l ray.io/cluster -w                       # KubeRay が worker を 1 つ作り直す
kubectl -n training logs "job/$WF" | grep -E "再開|world_size=|Worker group|restart" | tail
# … rank=0: チェックポイントから再開します（2 エポック終了済み）   ← Ray Train が最後のチェックポイントから続ける
```

予想される動き: Ray Train が失敗を検知し、ワーカーを全部作り直して、Ray が保存した最後のチェックポイント
（`s3://curated/checkpoints/ray/<名前>/…`）から続ける（`FailureConfig.max_failures: 2`）。RayJob 自体は失敗しない。

実際に観察した動きは、2 つの方式の違いとして [docs/architecture.md](../architecture.md) の「フェーズ 4」にまとめます。

## 13. 変換と同時に投げたときの貸し借り（完了条件 5）

フェーズ 3 で決めた枠（data-processing 2 CPU、training 2 CPU。互いに借りられる。取り返しはしない）で、次の順に投げます。

```bash
# 変換を 2 本（1 本目は自分の枠、2 本目は training の枠を 1.2 CPU 借りる）
for d in dev-021 dev-022; do argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=$d -p force=true; done
sleep 60
# 学習（TrainJob、requests 1 CPU / 3Gi）。training の枠は 1.2 CPU / 4Gi 貸し出し中で 0.8 CPU / 0.5Gi しか空いていないので、変換が 1 本終わるまで待つはず
argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=trainjob -p workers=2
```

記録するもの（監視の画面と、次のコマンド）:

```bash
for cq in data-processing training; do echo "== $cq"; kubectl get clusterqueue $cq \
  -o jsonpath='{range .status.flavorsUsage[*].resources[*]}{.name}{"\ttotal="}{.total}{"\tborrowed="}{.borrowed}{"\n"}{end}'; done
kubectl -n training get workloads -o custom-columns='NAME:.metadata.name,RESERVED:.status.conditions[?(@.type=="QuotaReserved")].status,REASON:.status.conditions[?(@.type=="QuotaReserved")].reason,MESSAGE:.status.conditions[?(@.type=="QuotaReserved")].message'
```

RayJob 版の学習は requests が 1.6 CPU / 約 5.8Gi（head の分だけ多い）なので、メモリが training の自分の枠（4.5Gi）では足りず、
data-processing の空き枠を借りないと動けません。同じ手順を `-p engine=rayjob` でも行い、待ち方の違いを記録します。

## 14. ワーカー 1 台と 2 台の比較（完了条件 6）

同じ条件（エポック数・バッチサイズ）で、ワーカー数だけを変えて実行します（データセットは同じなので、作成はスキップされる）。

```bash
for w in 1 2; do argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=trainjob -p workers=$w -p epochs=3 --wait; done
for w in 1 2; do argo submit -n mlops --from workflowtemplate/train-scene-classifier -p engine=rayjob -p workers=$w -p epochs=3 --wait; done
```

1 エポックあたりの学習時間（`epoch_train_seconds` の平均）を並べます（ホストから。MLflow は手順 6 の port-forward）。

```bash
(cd apps/trainer && MLFLOW_TRACKING_URI=http://localhost:5050 uv run python -c '
from mlflow import MlflowClient
c = MlflowClient(); e = c.get_experiment_by_name("scene-classifier")
for r in c.search_runs([e.experiment_id], order_by=["attributes.start_time DESC"], max_results=10):
    h = c.get_metric_history(r.info.run_id, "epoch_train_seconds")
    if h: print(r.data.params.get("engine"), "world_size", r.data.params.get("world_size"), "平均", round(sum(m.value for m in h) / len(h), 1), "秒/エポック", r.info.run_name)
')
```

考えるときの観点:

- 2 台にすると、1 台あたりのデータは半分になる。一方で、毎バッチの backward で勾配の all-reduce（gloo、ノード間の TCP 通信）が増える
- モデルが小さく（約 7 万パラメータ）計算が軽いほど、通信の割合が大きくなり、2 台にしても速くならないことがある
- RayJob 版は Ray Data の読み込み・デコードが学習と並行するぶん、torchrun 版と差が出ることがある

## 片付け

TrainJob と RayJob は Namespace training に残ります（別の Namespace なので、ワークフローを消しても消えない）。
**学習の Pod・ログが消える操作です。** MLflow とチェックポイント（`s3://curated/checkpoints/`）には記録が残ります。

```bash
kubectl -n training get trainjob,rayjob                     # 消すものを確認
kubectl -n training delete trainjob <名前>                   # 1 つずつ、確認しながら
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| build-dataset が `AccessDenied`（curated） | SeaweedFS の pipeline ユーザーに curated の権限が無い（手順 2 の Secret 更新・SeaweedFS の再起動を忘れた） |
| build-dataset が「別の中身で既にあります」で失敗 | 同じ `dataset-version` で、入力（フレームの中身・ラベル・セッションの増減）か作り方が変わった。データセットは書き換えないので、`-p dataset-version=3` のように新しい番号で作る。変換し直しただけ（`force=true`）ならフレームは同じなので、0.1.1 以降はスキップされる |
| build-dataset が「変換済みのセッションが 1 件もありません」 | 手順 1 の変換が終わっていない |
| port-forward で `error creating forwarding stream … Timeout occurred` が続く / 画面が開かない | 手元の同じポートを、ほかのアプリも待ち受けている（`lsof -nP -iTCP:<ポート> -sTCP:LISTEN` で確認）。別のポートで port-forward する |
| MLflow の Pod が `OOMKilled` / `CrashLoopBackOff` | MLflow 3.x の裏の仕事用のプロセスが起動している。`platform/manifests/mlflow/deployment.yaml` に `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` があるか確認して適用し直す |
| 学習の Pod の 1 台だけが `Pending`（`Insufficient cpu`）で、ログに suspend=True / False が 10 分ごとに繰り返される | Kueue は受け入れたが、1 台ごとの空きが足りずに Pod が載らず、waitForPodsReady で戻されている。`kubectl describe nodes \| grep -E "^Name:\|^  cpu "` で各ノードの空きを確認し、学習ワーカーの requests を空きより小さくする（今は 500m） |
| TrainJob の作成が `admission webhook "validator.trainjob.trainer.kubeflow.org" denied` | ランタイム `torch-cpu` が無い（手順 5）か、`numNodes` などの値が不正。メッセージを確認 |
| TrainJob がずっと `suspend: true`、training の Workload も無い | Kueue の TrainJob 連携が有効になっていない（手順 3 のログ）。Kueue を再起動（`kubectl -n kueue-system rollout restart deploy/kueue-controller-manager`）してから投げ直す |
| 学習の Pod が `CreateContainerConfigError`、`secret "training-s3-credentials" not found` | 手順 5 の Secret を作っていない |
| 学習のログに `ACCESS_DENIED during HeadObject`（`scene-cls/v1/manifest.json`、bucket `curated`） | Secret `training-s3-credentials` のキーが、SeaweedFS に登録したキー（`.env` の `TRAINING_AWS_*`）と違う。`.env` を埋める前に手順 5 を実行した場合に起きる。手順 5 の Secret を作り直す |
| 学習のログに `Invalid Host header - possible DNS rebinding attack detected` | MLflow の `--allowed-hosts` に、学習の Pod が使うホスト名（`mlflow.mlflow.svc:5000`）が入っていない |
| torchrun が `Connection refused` / `Timed out ... rendezvous` で失敗 | もう 1 台の Pod がまだ起動していない（イメージの取得中など）。JobSet が作り直すので、多くは自動で解消する。続くなら `kubectl -n training describe pod` で Pending の理由を確認 |
| 学習が止まって進まない（ログが epoch の途中で止まる） | rank ごとのバッチ数がずれて、DDP の待ち合わせで止まっている可能性。学習器は全 rank のバッチ数を揃えているので、イメージが古くないか（タグ）を確認 |
| RayJob の学習で `Connection closed by peer … remote worker crashing`、ログに `OOM kill reason: Memory usage … exceeded threshold` | 学習ワーカー 2 つが同じ worker の Pod に置かれ、メモリが足りなくなった（ログの `rank=0` と `rank=1` の `host` が同じ）。entrypoint に `--placement-strategy=STRICT_SPREAD` があるか、学習器のイメージが 0.1.1 以降かを確認 |
| RayJob の学習が `Waiting for resources`、Ray Data の読み込みが進まない | worker の Ray から見た CPU（`num-cpus: "2"`）が 1 になっていると、学習のワーカーが CPU を占有して Ray Data のタスクが動けない。テンプレートを確認 |
| register が「run が見つかりません」 | 学習が MLflow に記録できていない（`MLFLOW_TRACKING_URI`、MLflow サーバーの状態を確認）。`kubectl -n mlflow logs deploy/mlflow` |
| register が「登録しません」で終わる | 正解率が閾値に届かなかった（ワークフローは成功扱い）。エポック数を増やすか、`-p threshold=` を見直す |
| `trainer trace` が `NETWORK_CONNECTION … Failed to connect to localhost port 8333` | S3 の port-forward が動いていない（SeaweedFS の再起動で切れた等）。`kubectl -n storage port-forward svc/seaweedfs-all-in-one 8333:8333` |
| `trainer trace` が `NoSuchBucket` / 接続エラー | ホストの `.env` を読み込み、S3 の port-forward（8333）と MLflow の port-forward（5000）が動いているか確認 |
