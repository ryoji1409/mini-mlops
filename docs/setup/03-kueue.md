# フェーズ 3: Kueue によるジョブのキューイング

変換ジョブ（mcap-convert の RayJob）を Kueue のキュー経由にします。
クォータ（CPU / メモリの枠）が空いているときだけジョブを受け入れ、溢れた分は Ray の Pod を 1 つも作らずに待たせます。
キューの設計（なぜこのクォータ値か）は [docs/architecture.md](../architecture.md) の「フェーズ 3」、
部品の構成は [platform/README.md](../../platform/README.md)、ワークフローは [pipelines/README.md](../../pipelines/README.md)。

**完了条件**:

1. mcap-convert の RayJob が Workload として受け入れられ、変換と検証が成功する（手順 6）
2. クォータを超える数の変換を投げると、一部は Workload が Pending のまま待ち、Ray の Pod は 1 つも作られない。前が終わると順番に受け入れられる（手順 7）
3. 待ち行列で low の後に high を投げると、空きが出たときに high が先に受け入れられる（手順 9）
4. training が空いているときは、data-processing が training の枠を借りて同時に 2 本動く（手順 7・8）
5. queue-name の無い Pod / Job は Kueue の影響を受けない（手順 10）

## 前提

- フェーズ 2（[02-pipeline.md](02-pipeline.md)）が終わっている
- `.env` を読み込み、S3 の port-forward が動いている（[01-storage.md](01-storage.md) 手順 4）

```bash
set -a; source .env; set +a
```

## 1. 変換器のイメージ（0.2.1）をビルドして push する

フェーズ 3 で変換器に `--device-id`（デバイスで対象を絞る。raw に無いデバイスを指定したらエラー）を足したので、バージョンを 0.2.1 に上げています。
[02-pipeline.md](02-pipeline.md) 手順 6 と同じコマンドで、新しいタグのイメージを作ります（VERSION は pyproject.toml から読むので 0.2.1 になる）。

```bash
VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/converter/pyproject.toml); echo "$VERSION"    # 0.2.1
IMAGE="ghcr.io/${GHCR_USER}/mini-mlops-converter:${VERSION}"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
docker buildx build \
  --platform "${CONVERTER_PLATFORM:-linux/amd64,linux/arm64}" \
  --build-arg CONVERTER_VERSION="$VERSION" \
  --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "$IMAGE" --push apps/converter
```

**変換器の版が変わったので、前の版で変換済みのセッションは、次にそのデバイスを対象に実行したとき作り直されます**
（マニフェストに記録した版と違う = 変換対象、というフェーズ 2 からの決まり）。

## 2. Kueue を入れる

```bash
helmfile -f platform/helmfile.yaml -l name=kueue diff
helmfile -f platform/helmfile.yaml -l name=kueue apply       # webhook が動き出すまで待つ（数分）
kubectl -n kueue-system get pod
```

期待される出力:

```
NAME                                        READY   STATUS    RESTARTS   AGE
kueue-controller-manager-xxxxxxxxxx-xxxxx   1/1     Running   0          1m
```

Kueue の webhook が `mlops` と `training` だけを見ていることを確かめます（基盤部品の Namespace には関わらない）。

```bash
kubectl get mutatingwebhookconfiguration kueue-mutating-webhook-configuration \
  -o jsonpath='{range .webhooks[*]}{.name}{"\t"}{.failurePolicy}{"\t"}{.namespaceSelector.matchExpressions[0].values}{"\n"}{end}' | grep -E "mjob|mrayjob|mpod"
```

期待される出力:

```
mjob.kb.io      Fail    ["mlops","training"]
mpod.kb.io      Ignore  ["mlops","training"]       ← Pod 連携は使わないので、Kueue が落ちていても Pod の作成は止めない
mrayjob.kb.io   Fail    ["mlops","training"]
```

## 3. キューを作る

```bash
kubectl apply -k platform/manifests/kueue
kubectl get resourceflavor,cohort,workloadpriorityclass
kubectl get clusterqueue -o wide
kubectl get localqueue -A
```

期待される出力:

```
NAME                                         AGE
resourceflavor.kueue.x-k8s.io/cpu-standard   10s
NAME                          AGE
cohort.kueue.x-k8s.io/mlops   10s
NAME                                               VALUE
workloadpriorityclass.kueue.x-k8s.io/high          1000
workloadpriorityclass.kueue.x-k8s.io/low           100

NAME              COHORT   STRATEGY         PENDING WORKLOADS   ADMITTED WORKLOADS   ACTIVE   REASON
data-processing   mlops    BestEffortFIFO   0                   0                    True     Ready
training          mlops    BestEffortFIFO   0                   0                    True     Ready

NAMESPACE   NAME              CLUSTERQUEUE      PENDING WORKLOADS   ADMITTED WORKLOADS
mlops       data-processing   data-processing   0                   0
training    training          training          0                   0
```

`ACTIVE` が `True` でなければ、`kubectl describe clusterqueue <名前>` の Conditions に理由が出ます（ResourceFlavor が無い、など）。

## 4. WorkflowTemplate を登録し直す

フェーズ 3 版のテンプレート（Kueue のラベル、`suspend: true`、`device-id` / `priority` / `queue-name` パラメータ）を登録します。

```bash
sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" pipelines/mcap-convert.yaml | kubectl apply -f -
argo template get -n mlops mcap-convert | grep -A12 Parameters
```

## 5. 確認用のデータを追加する

並行実行を確かめるため、既存の 6 本（dev-001 / dev-002）と ID が重ならないデバイス dev-011〜dev-014 を 4 台 × 5 セッション作ってアップロードします。

```bash
(cd apps/mcap-generator && uv run mcap-generate --first-device 11 --devices 4 --sessions-per-device 5 --out ../../output --seed 7)
scripts/validate-mcap.sh output                 # 最後に OK=26 NG=0（既存 6 + 追加 20）
scripts/upload-raw.sh output                    # 既存の 6 本は「スキップ」、追加の 20 本がアップロードされる
aws s3 ls s3://raw/ | sort                      # PRE device=dev-001/ … PRE device=dev-014/
```

隔離の確認で置いた `dev-901` が raw に残っている場合、全デバイスを対象にした実行では隔離済みとして扱われます（影響はない）。

## 6. 1 本だけ実行する（完了条件 1）

別のターミナルで、キューと Workload の様子を 5 秒ごとに表示しておきます（Ctrl-C で終了）。

```bash
while true; do clear; date
  kubectl get clusterqueue -o wide
  kubectl -n mlops get workloads -o custom-columns='NAME:.metadata.name,PRIORITY:.spec.priorityClassRef.name,QUEUE:.spec.queueName,ADMITTED:.status.conditions[?(@.type=="Admitted")].status,FINISHED:.status.conditions[?(@.type=="Finished")].status'
  kubectl -n mlops get rayjob -o custom-columns='NAME:.metadata.name,DEVICE:.metadata.labels.mini-mlops/device-id,SUSPEND:.spec.suspend,DEPLOY:.status.jobDeploymentStatus,JOB:.status.jobStatus'
  kubectl -n mlops get pod -l ray.io/cluster --no-headers 2>/dev/null | awk '{print $1, $3}'
  sleep 5; done
```

元のターミナルで dev-011 だけを変換します。

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=dev-011 --watch
```

監視の画面では次の順に変わります。

| 段階 | Workload | RayJob（SUSPEND / DEPLOY） | Ray の Pod |
|---|---|---|---|
| RayJob を作った直後 | `ADMITTED` が空（まだ判定前） | `true` / `Suspended` | なし |
| Kueue が受け入れた | `ADMITTED=True` | `false` / `Initializing` → `Running` | head 1 + worker 2 + submitter 1 |
| 変換が終わった | `FINISHED=True` | `false` / `Complete`、JOB は `SUCCEEDED` | すぐに消える |

Workload の中身を見ます（受け入れたときにクォータをいくつ使ったか）。

```bash
WL=$(kubectl -n mlops get workloads --sort-by=.metadata.creationTimestamp -o name | tail -1)
kubectl -n mlops describe "$WL"
```

読み方（抜粋）:

```
Spec:
  Pod Sets:                                  ← Kueue が数えた Pod のまとまり（head / worker / submitter）
    Count:  1    Name: head                  ← requests: cpu 500m, memory 2560Mi
    Count:  2    Name: cpu                   ← worker 2 つ（500m / 1Gi ずつ）
    Count:  1    Name: submitter             ← ray job submit をする Pod（100m / 256Mi）も枠に数える
  Priority:  100                             ← low
  Queue Name:  data-processing
Status:
  Admission:
    Cluster Queue:  data-processing
    Pod Set Assignments:                     ← どの ResourceFlavor の枠を使ったか
      Flavors:  cpu: cpu-standard, memory: cpu-standard
      Resource Usage:  cpu: 500m, memory: 2560Mi       （head 分。worker / submitter も同様に並ぶ）
  Conditions:
    Type: QuotaReserved   Status: True  Reason: QuotaReserved   ← 枠を確保した
    Type: Admitted        Status: True  Reason: Admitted        ← 受け入れた（suspend が外れる）
    Type: PodsReady       Status: True                          ← 全 Pod が揃った（waitForPodsReady）
    Type: Finished        Status: True  Reason: Succeeded       ← 終わった（枠が返る）
```

ワークフローの結果は、フェーズ 2 と同じように確かめます（dev-011 の 5 本だけが対象）。

```bash
argo logs -n mlops @latest | grep -E "変換対象|検証結果"
# セッション 5 件: 変換対象 5 / 変換済み 0 / 隔離済み 0 / metadata 不正 0
# 検証結果: {"sessions": 5, "ok": 5, ...}
```

## 7. 待ち行列と貸し借り（完了条件 2・4）

4 デバイス分を同時に投げます。`force=true` は、手順 6 で変換済みの dev-011 も作り直して、4 本とも実際に Ray を動かすためです。

```bash
for d in dev-011 dev-012 dev-013 dev-014; do
  argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=$d -p force=true
done
```

監視の画面で次のことを確かめます。

- **2 本だけが受け入れられ（`ADMITTED=True`）、残り 2 本は待つ**。待っている RayJob は `SUSPEND=true` / `Suspended` で、**Ray の Pod が 1 つも無い**（全部か無しか）
- `kubectl get clusterqueue -o wide` の data-processing が `PENDING WORKLOADS 2`、`ADMITTED WORKLOADS 2`
- 1 本終わる（`FINISHED=True`）と、待っていた 1 本がすぐ受け入れられる

**2 本目が動けるのは、training の枠を借りているからです**（data-processing の自分の枠は 1 本分）。借りている量を見ます。

```bash
kubectl get clusterqueue data-processing \
  -o jsonpath='{range .status.flavorsUsage[*].resources[*]}{.name}{"\ttotal="}{.total}{"\tborrowed="}{.borrowed}{"\n"}{end}'
```

期待される出力（2 本動いているとき）:

```
cpu     total=3200m    borrowed=1200m     ← 自分の枠 2 を超えた 1.2 CPU を training から借りている
memory  total=9728Mi   borrowed=4096Mi    ← 同じく 4Gi を借りている（自分の枠 5.5Gi = 5632Mi）
```

待っている Workload の並び順は、Kueue の visibility API で見られます。

```bash
kubectl get --raw "/apis/visibility.kueue.x-k8s.io/v1beta2/clusterqueues/data-processing/pendingworkloads" | python3 -m json.tool | grep -E '"name"|positionInClusterQueue|priority'
```

待っている Workload の理由は、QuotaReserved 条件に出ます（v0.20 では Reason が `WaitingForQuota`）。

```bash
kubectl -n mlops get workloads -o custom-columns='NAME:.metadata.name,RESERVED:.status.conditions[?(@.type=="QuotaReserved")].status,REASON:.status.conditions[?(@.type=="QuotaReserved")].reason,MESSAGE:.status.conditions[?(@.type=="QuotaReserved")].message'
# 例: rayjob-mcap-convert-xxxxx-xxxxx  False  WaitingForQuota  couldn't assign flavors to pod set head: insufficient unused quota for cpu ...
#     ↑ 枠（Cohort 全体 4 CPU）の残りが足りないので待っている。ノードの空きではなく、クォータで止めている（文言は版によって少し違う）
```

順番に受け入れられたことは、RayJob の START TIME（KubeRay が受け入れ時に付ける開始時刻）と、先に動いていた RayJob の END TIME を比べて確かめます。

```bash
kubectl -n mlops get rayjob --sort-by=.status.startTime
# 待っていた RayJob の START TIME が、先の RayJob の END TIME の 1 秒ほど後になっていれば、空きが出た直後に受け入れられている。
# 待っている間の RayJob は DEPLOYMENT STATUS が Suspended、RAY CLUSTER NAME が空（Ray クラスタはまだ作られていない）。
# START TIME は作成時刻が入っているが、受け入れられた時点で付け直される（KubeRay の仕様）
```

4 本とも終わったら、ワークフローがすべて成功していることを確かめます。

```bash
argo list -n mlops | head -6          # STATUS がすべて Succeeded
```

## 8. 借りられないときは 1 本ずつになる（完了条件 4 の比較）

training に枠を埋めるダミーの Job（4 Pod × 500m / 1Gi = 2 CPU / 4Gi）を入れて、training が「使っている」状態にします。
これは `training` の LocalQueue に入る batch/v1 Job なので、Job の連携の確認も兼ねます。

```bash
kubectl create -f platform/manifests/kueue-check/training-filler.yaml
kubectl -n training get workloads                     # ADMITTED が True
kubectl -n training get pod                           # training-filler-xxxxx が 4 つ Running
```

この状態で 3 デバイス分を投げます。

```bash
for d in dev-012 dev-013 dev-014; do
  argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=$d -p force=true
done
```

- 今度は **1 本ずつしか受け入れられない**（data-processing の自分の枠 1 本分だけ。training の枠は使用中なので借りられない）
- `borrowed` は 0 のまま

```bash
kubectl get clusterqueue -o wide                       # data-processing: PENDING 2 / ADMITTED 1
```

続けて手順 9（優先度）を、ダミーの Job を残したまま行います（1 本ずつの状態のほうが、順番の違いが分かりやすい）。

## 9. 優先度（完了条件 3）

手順 8 の 3 本が 1 本ずつ動いている間に、**low をもう 1 本、その後に high を 1 本** 投げます。

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=dev-011 -p force=true -p priority=low
sleep 10
argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=dev-001 -p force=true -p priority=high
```

待ち行列の並びを見ます。

```bash
kubectl get --raw "/apis/visibility.kueue.x-k8s.io/v1beta2/clusterqueues/data-processing/pendingworkloads" \
  | python3 -c 'import json,sys; [print(w["positionInClusterQueue"], w["priority"], w["metadata"]["name"]) for w in json.load(sys.stdin)["items"]]'
```

期待される出力（先頭が次に受け入れられる）:

```
0 1000 rayjob-mcap-convert-xxxxx-xxxxx     ← 最後に投げた high（dev-001）が先頭
1 100  rayjob-mcap-convert-yyyyy-yyyyy     ← 先に投げた low たち（手順 8 の残り 2 本と dev-011）は古い順に後ろへ
2 100  rayjob-mcap-convert-zzzzz-zzzzz
3 100  rayjob-mcap-convert-wwwww-wwwww
```

今動いている 1 本が終わると、high が次に受け入れられることを監視の画面で確かめます。
受け入れられた順は、Workload の Admitted 条件の時刻で比べられます。
待っている Workload にも `Admitted=False` の条件が付いていて時刻が入るので、**ADMITTED が True の行だけ** を見ます。

```bash
kubectl -n mlops get workloads --sort-by='.status.conditions[?(@.type=="Admitted")].lastTransitionTime' \
  -o custom-columns='NAME:.metadata.name,PRIORITY:.spec.priorityClassRef.name,ADMITTED:.status.conditions[?(@.type=="Admitted")].status,AT:.status.conditions[?(@.type=="Admitted")].lastTransitionTime'
```

すべて終わったら、ダミーの Job を消します（training の枠が空き、data-processing が借りられる状態に戻る）。

```bash
kubectl -n training delete job training-filler
```

## 10. Kueue が他のものに干渉しないこと（完了条件 5）

queue-name ラベルの無い Job を、Kueue が管理する Namespace（training）に作ります。

```bash
kubectl create -f platform/manifests/kueue-check/no-queue-job.yaml
kubectl -n training wait --for=condition=Complete job/no-queue-job --timeout=2m
kubectl -n training logs job/no-queue-job                  # Kueue を通らずに動きました
kubectl -n training get workloads                          # no-queue-job の Workload は無い
kubectl -n training get job no-queue-job -o jsonpath='{.spec.suspend}{"\n"}'    # false（Kueue が suspend にしていない）
```

Argo の discover / validate の Pod（ラベル無し）や基盤部品も Workload になっていないことを確かめます。

```bash
kubectl get workloads -A                                   # rayjob-mcap-convert-… と job-training-filler-… だけ
kubectl get pod -A | grep -v -E "Running|Completed"        # 何も出ない（Pending / Error の Pod が無い）
```

## 11.（任意）プリエンプションの観察

この検証の運用方針（待たせるだけ）では使いませんが、「優先度の高いジョブが低いジョブを追い出す」と RayJob がどうなるかを、別のキューで観察します。
観察中は data-processing に変換を投げないでください（このキューは Cohort の外なので、両方動くとクォータの合計がノードの空きを超える）。

```bash
kubectl apply -k platform/manifests/kueue-preemption-test
kubectl get clusterqueue preemption-test -o wide

# low を 1 本（変換 1 本分の枠を使い切る）
argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=dev-012 -p force=true -p priority=low -p queue-name=preemption-test
# low の RayJob が Running になったら（監視の画面で確認）、high を 1 本
argo submit -n mlops --from workflowtemplate/mcap-convert -p device-id=dev-013 -p force=true -p priority=high -p queue-name=preemption-test
```

観察すること:

```bash
kubectl -n mlops get rayjob -w -o custom-columns='NAME:.metadata.name,DEVICE:.metadata.labels.mini-mlops/device-id,SUSPEND:.spec.suspend,DEPLOY:.status.jobDeploymentStatus'
kubectl -n mlops describe workloads | grep -E "Name:|Evicted|Preempted" 
kubectl -n mlops get events --sort-by=.lastTimestamp | grep -i -E "preempt|evict|suspend" | tail
```

実際に観察した動き（KubeRay v1.7.2 のコードから予想したとおり）:

1. Kueue が low の Workload を追い出し（`Evicted`、理由 `Preempted`）、low の RayJob の `suspend` を `true` に戻す
2. KubeRay が low の Ray クラスタと submitter を **削除** し、RayJob を `Suspending` → `Suspended` にする（実行中の変換は途中で止まる）
3. high が受け入れられて動く
4. high が終わると low が再び受け入れられ、**Ray クラスタを作り直して最初から** 実行する（`status.startTime` も付け直される）。
   ただし変換器はセッションごとにマニフェストを書くので、追い出される前に書き終えたセッションは「変換済み」としてスキップされる
5. low のワークフローは失敗せずに待ち続け、最後は成功する（`Suspended` は failureCondition に当たらない）

low の RayJob の状態の移り変わり（`kubectl get rayjob -w`）:

```
mcap-convert-<low>    Initializing                     ← 動いている
mcap-convert-<high>   true   Initializing → Suspending → Suspended   ← high は作られた直後、いったん待ち（通常どおり）
mcap-convert-<low>    true   Initializing → Suspending → Suspended   ← 追い出された（SUSPEND が true に戻る。Ray クラスタは削除）
mcap-convert-<high>   Initializing → Running → Complete             ← high が動く
mcap-convert-<low>    Initializing → …                              ← 空いたので再受け入れ。Ray クラスタを作り直して最初から
```

Kueue のイベント（`kubectl -n mlops get events`）:

```
Normal   Preempted                     Preempted to accommodate a workload ... preemptor effective priority: 1000 ...; preemptee effective priority: 100
Warning  WaitingForPreemptedWorkloads  couldn't assign flavors to pod set head: insufficient unused quota ... Pending the preemption of 1 workload(s)
```

追い出された low の Ray の Pod が消えきるまでの数十秒、high の Pod が `Insufficient cpu` で Pending になることがあります
（Kueue は追い出しを決めた時点で枠を移すが、Pod の終了には時間がかかる）。消えれば自動で載ります。

観察が終わったら、プリエンプション用のキューを消して元に戻します（キューの定義だけを消す操作。データは消えない）。

```bash
kubectl get workloads -n mlops -o custom-columns='NAME:.metadata.name,QUEUE:.spec.queueName,FINISHED:.status.conditions[?(@.type=="Finished")].status' | grep preemption-test
                                                           # すべて FINISHED=True であることを確認してから
kubectl delete -k platform/manifests/kueue-preemption-test
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `kubectl apply -k platform/manifests/kueue` が `failed calling webhook "vclusterqueue.kb.io" ... connection refused` | Kueue の webhook がまだ起動していない。`kubectl -n kueue-system rollout status deploy/kueue-controller-manager` を待ってから再実行 |
| RayJob がずっと `Suspended`、`kubectl -n mlops get workloads` に何も無い | Workload が作られていない。RayJob の `kueue.x-k8s.io/queue-name` ラベルと LocalQueue の名前（`kubectl -n mlops get localqueue`）が一致しているか、Kueue のログ（`kubectl -n kueue-system logs deploy/kueue-controller-manager`）を確認 |
| Workload が Pending のまま、Message に `LocalQueue ... doesn't exist` / `ClusterQueue ... is inactive` | 手順 3 のキューが作られていない / ResourceFlavor が無い。`kubectl get clusterqueue -o wide` の `ACTIVE` を確認 |
| Workload の Message に `WorkloadPriorityClass ... not found` | `-p priority=` に high / low 以外を指定した（ワークフローのパラメータは enum で high / low に限っている） |
| 受け入れられたのに Ray の worker が `Pending`、`describe pod` に `Insufficient cpu`。終わったはずの RayJob の Ray の Pod が残っている | Kueue はジョブ終了と同時に枠を返すが、Ray クラスタが残っているとノードの CPU が空かない。RayJob の `ttlSecondsAfterFinished` が 0 になっているか確認（以前の 120 では実際に起きた）。古いテンプレートで投げた分は、残った Ray クラスタが消えれば自動で載る |
| 受け入れられたのに Ray の Pod が `Pending`（ノードの空きが足りない） | クォータより実際のノードの空きが小さい（他の Pod が増えた等）。10 分で Kueue が追い出して並び直させる（waitForPodsReady）。`kubectl describe node` で空きを確認し、クォータ（`platform/manifests/kueue/clusterqueue-*.yaml`）を下げる |
| Workload が `Evicted`（理由 `PodsReadyTimeout`）を繰り返し、最後に `Active: False` | 受け入れ後 10 分以内に Ray クラスタが Ready にならなかった（イメージの取得に時間がかかった等）を 3 回繰り返した。原因を直してから、ワークフローを止めて投げ直す |
| discover が失敗、ログに `raw にセッションが 1 件も無いデバイスが指定されました: dev-0xx` | `device-id` の打ち間違い。`aws s3 ls s3://raw/` で実在するデバイスを確認する（以前は対象 0 件のまま成功していた） |
| ワークフローが `Pending`、`waiting for lock` | 同じデバイスのワークフローが動いている（デバイスごとに 1 つずつ）。`argo list -n mlops` で確認 |
| ワークフローが 6 時間で `Failed`（`Step exceeded its deadline`） | Kueue での待ちが長すぎた。`kubectl get clusterqueue -o wide` で詰まっていないか、`describe workloads` の Message を確認 |
| `kubectl` のたびに `couldn't get resource list for visibility.kueue.x-k8s.io/v1beta2` の警告 / Namespace の削除が終わらない | Kueue の Pod が落ちていて、Kueue の visibility API（APIService）が応答しない。Kueue を復旧させる（`kubectl -n kueue-system get pod`） |
| training-filler の Pod が `Pending` | ノードの空きが足りない（変換が 2 本動いている最中に作った等）。変換が終わるのを待つ |
