# 構成とデータの流れ

## フェーズ 0: ストレージの土台（Longhorn）

```
┌──────────────────── Kubernetes ────────────────────┐
│ ns longhorn-system:                                                                            │
│   longhorn-manager（DaemonSet、全ノード） … ボリュームの管理、各ノードの /var/lib/longhorn を使う │
│   instance-manager（ノードごと）        … ボリュームの engine / replica プロセス（データの通り道）│
│   CSI（provisioner など各 1 + csi-plugin DaemonSet） … PVC → Longhorn ボリュームの作成・接続      │
│   longhorn-ui（1、port-forward :8090）                                                          │
│                                                                                                │
│ StorageClass longhorn（default、レプリカ 2）                                                    │
│   PVC ──▶ Longhorn ボリューム ──▶ レプリカ 2 つを別々のノードのディスクに置く（iSCSI で接続）   │
│   使う側: SeaweedFS（フェーズ 1）、MLflow など（後のフェーズ）                                  │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## フェーズ 1 時点

```
┌──────────────────────────── ホスト ────────────────────────────┐
│                                                                     │
│  apps/mcap-generator          scripts/validate-mcap.sh              │
│  (uv run mcap-generate)  ──▶  sha256・サイズ照合                    │
│        │                      mcap doctor（構造チェック）           │
│        ▼                      mcap info ⇔ metadata.json 照合        │
│  output/device=…/date=…/              │ OK のものだけ             │
│    session_id=…/{<id>.mcap, metadata.json}│                           │
│                                         ▼                           │
│                               scripts/upload-raw.sh                 │
│                               (aws s3 cp + x-amz-meta-sha256)       │
└─────────────────────────────────────────┬───────────────────────────┘
                                          │ kubectl port-forward
                                          │ localhost:8333
┌──────────────── Kubernetes ─────────────▼───────────────────────────┐
│  namespace: storage                                                 │
│   deploy/seaweedfs-all-in-one（master+volume+filer+S3, 1 Pod）      │
│     ├─ Secret seaweedfs-s3-config（S3 アクセスキー）                │
│     └─ PVC seaweedfs-all-in-one-data（Longhorn 5Gi）                │
│                                                                     │
│   バケット: raw / derived / curated / mlflow / vectors              │
└─────────────────────────────────────────────────────────────────────┘
```

## raw ゾーンのキー設計

```
s3://raw/device=<device_id>/date=<YYYY-MM-DD(UTC)>/session_id=<session_id>/<session_id>.mcap
s3://raw/device=<device_id>/date=<YYYY-MM-DD(UTC)>/session_id=<session_id>/metadata.json
```

- Hive 形式（`key=value`）のパーティションなので、後のフェーズで DuckDB / Ray Data / Spark がそのまま読める。
- `date` はセッション開始時刻の **UTC** 日付（タイムゾーンで分割先が揺れないように）。
- `metadata.json` は最後に置く ＝ 「このセッションのアップロードは完了」の目印。後段の処理は metadata.json を起点にセッションを見つける。

## フェーズ 2 時点

```
┌──────── ホスト ────────┐        ┌──────────── GHCR（非公開） ────────────┐
│ docker buildx build     │ push   │ ghcr.io/<user>/mini-mlops-converter:0.2.1│
│   --push ───────────────┼───────▶│ （タグ = 変換器のバージョン）            │
│  argo submit            │        └─────────────────────┬───────────────────┘
└──────────┬──────────────┘                              │ pull（Secret ghcr-pull）
           │ kubectl / argo                              │
┌──────────▼────────────────────────── Kubernetes ─────────▼───────────────────────────────┐
│ ns argo:        argo-workflows-workflow-controller / argo-workflows-server（UI :2746）    │
│ ns ray-system:  kuberay-operator                                                          │
│ ns mlops:       Workflow mcap-convert（SA mcap-pipeline）                                 │
│                  ① discover Pod ──────────────┐                                          │
│                  ② RayJob ─▶ RayCluster        │ S3 API（pipeline ユーザー）               │
│                       head（driver: converter convert, Dashboard :8265）                  │
│                       worker × 2（preflight / read_mcap / 変換 / 書き込み）               │
│                       submitter Job（ray job submit、ログ）                               │
│                  ③ validate Pod（DuckDB）─────┤                                          │
│ ns storage:     SeaweedFS ◀────────────────────┘                                          │
│                  raw（読み取りのみ） → derived（Parquet・マニフェスト） / argo-artifacts（ログ）│
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

### derived ゾーンのキー設計

```
s3://derived/telemetry/imu/device=<v>/date=<d>/session_id=<id>/part-00000.parquet
s3://derived/telemetry/gnss/device=<v>/date=<d>/session_id=<id>/part-00000.parquet
s3://derived/frames/front/device=<v>/date=<d>/session_id=<id>/part-NNNNN.parquet   （32MB で分割）
s3://derived/_manifests/<session_id>.json      系譜: 入力の s3 パス・sha256、変換器の版（= イメージタグ）、出力ファイル（行数・sha256）、トピックごとの行数
s3://derived/_quarantine/<session_id>.json     変換できなかったセッション: 段階（preflight / read / convert）、エラー、入力 sha256、変換器の版
```

- 下流（フェーズ 4・5）は **マニフェストのあるセッションだけ** を読む（マニフェスト = 完了の印）。
- 1 セッション = 1 パーティションなので、`--force` での作り直しや隔離が他のセッションに影響しない。
  複数セッションをまとめた大きなシャードは、curated（学習用データセット、バージョン付き）を作るときに作る。

### 冪等性の判定

| derived の状態 | 判定 |
|---|---|
| マニフェストあり・入力 sha256 と変換器の版が一致 | 変換済み → スキップ |
| 隔離記録あり・入力 sha256 と変換器の版が一致 | 隔離済み → スキップ（`force=true` で再挑戦） |
| それ以外（記録なし、raw の MCAP が差し替わった、変換器の版を上げた） | 変換する |

## フェーズ 3 時点（Kueue）

```
┌──────── ホスト ────────┐
│ argo submit             │  -p device-id=dev-011 -p priority=high など
└──────────┬──────────────┘
┌──────────▼─────────────────────────────── Kubernetes ─────────────────────────────────────┐
│ ns mlops:  Workflow mcap-convert（デバイスごとの mutex で、同じデバイスは 1 つずつ）          │
│              ② RayJob（suspend: true、queue-name / priority-class ラベル）                │
│                   │ ラベル付きのジョブだけ                                                 │
│ ns kueue-system:  ▼                                                                        │
│   kueue-controller-manager ── Workload を作る ── クォータが空いたら受け入れ → suspend: false │
│                                                                                            │
│   Cohort mlops（合計 4 CPU / 10Gi。互いに空き枠を貸し借りする）                              │
│   ├─ ClusterQueue data-processing  2 CPU / 5.5Gi  ◀── LocalQueue mlops/data-processing      │
│   └─ ClusterQueue training         2 CPU / 4.5Gi  ◀── LocalQueue training/training（フェーズ 4）│
│   ResourceFlavor cpu-standard（ワーカー 5 台をまとめて 1 種類）                              │
│   WorkloadPriorityClass high(1000) / low(100)                                               │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

### キューの設計判断

#### クォータの値

Kueue は Pod の **requests** の合計でクォータを数えます。変換 1 本（RayJob）の requests は次のとおりです。

| Pod | CPU | メモリ |
|---|---|---|
| head | 500m | 2560Mi |
| worker × 2 | 1000m | 2Gi |
| submitter（Kueue は submitter も数える） | 100m | 256Mi |
| **合計** | **1.6 CPU** | **4.75Gi** |

ワーカー 5 台（各 2 CPU / 約 3.8Gi）の実際の空きは、既存の Pod の requests を引くと **CPU 約 5.1 / メモリ約 15.3Gi**。
ここから Kueue 本体（100m / 256Mi）と、Kueue で待っている間も残る Argo の見張り用 Pod（1 本あたり 110m / 192Mi、4 本で約 0.44 CPU）を引くと、
使えるのは **CPU 約 4.5 / メモリ約 14Gi** です。**先に足りなくなるのは CPU** です。

| ClusterQueue | nominalQuota | borrowingLimit | 意味 |
|---|---|---|---|
| data-processing | 2 CPU / 5.5Gi | 2 CPU / 4.5Gi | 自分の枠で変換 1 本。training が空いていれば借りて 2 本 |
| training | 2 CPU / 4.5Gi | 2 CPU / 5.5Gi | README のメモリ予算「学習 4GB」に合わせる |
| Cohort の合計 | 4 CPU / 10Gi | | 変換 2 本（3.2 CPU / 9.5Gi）は入り、3 本目（4.8 CPU）は入らない |

- **クォータの合計（4 CPU）を、実際の空き（約 4.5 CPU）より小さくしてある**のがポイントです。
  Kueue は「枠」しか見ないので、枠が実際の空きより大きいと、受け入れたのにノードに載らない（Pod が Pending のまま、全部か無しかにならない）ことが起きます。
  枠の段階で止めれば、受け入れたジョブの Pod は必ず載ります。
- 念のため **waitForPodsReady**（受け入れ後 10 分で Ray クラスタが Ready にならなければ、いったん戻して並び直す）も有効にしています。
  ノードの不調などで載らなかったときに、枠だけ取ったジョブが他を止め続けないための保険です。
- メモリの合計 10Gi は、README のメモリ予算（Ray 6GB + 学習 4GB）とも合います。
- **注意**: ノードの limits の合計は既に allocatable を超えています（一部のワーカーでメモリが 157%）。Kueue は requests しか見ないので、
  全 Pod が limits まで使うとノードのメモリが足りなくなります。実際の使用量はワーカー 1 台あたり 1.3〜1.5Gi なので、今は余裕があります。

#### 貸し借りとプリエンプション

- 2 つの ClusterQueue を Cohort `mlops` に入れ、相手が使っていない枠を借りられるようにしています（変換が集中する時間帯に、学習の空き枠を使う）。
- **プリエンプションは使いません**（`withinClusterQueue: Never`、`reclaimWithinCohort: Never`）。この検証の運用方針として「待たせるだけ」にしています。
  - その結果、data-processing が training の枠を借りて変換している間に学習を投げると、学習はその変換が終わるまで待ちます
    （貸した枠を、相手のジョブを止めて取り返すことはしない）。変換は数分で終わるので、これでよしとします。
  - 優先度はプリエンプションが無くても効きます。待ち行列は「優先度の高い順 → 同じなら古い順」に並ぶので、空きが出たら high が先に入ります。
- 追い出したときの動き（RayJob は Ray クラスタごと消され、再受け入れ時に最初からやり直し）は、別のキュー `preemption-test` で観察だけします（docs/setup/03-kueue.md 手順 11）。

#### ResourceFlavor を 1 つにした理由

クラウドでよくある「オンデマンドと Spot の使い分け」は、ノードにラベルを付けて ResourceFlavor を 2 つに分け、ClusterQueue の flavors に順番に並べれば表現できます
（前の flavor の枠が埋まったら次を使う）。ただ、このクラスタのワーカーは 5 台しかなく、分けるとそれぞれの枠が変換 1 本分（1.6 CPU）に満たなくなります。
Spot の中断も再現できないので、今は `cpu-standard` 1 つにしています。

#### Kueue を基盤部品に関わらせない

- `manageJobsWithoutQueueName: false`: `kueue.x-k8s.io/queue-name` ラベルの無いジョブは管理しない。
- `managedJobsNamespaceSelector`: Kueue の webhook が見る Namespace を `mlops` と `training` に限る。
  チャートの既定では kube-system 以外のほぼ全 Namespace の Job 作成が Kueue の webhook（`failurePolicy: Fail`）を通るので、
  Kueue の Pod が落ちている間は longhorn-system や argo でも Job を作れなくなります。webhook の対象そのものを絞って、これを防いでいます。
- 使う連携は `batch/job`・`ray.io/rayjob`・`trainer.kubeflow.org/trainjob` の 3 つだけ（Pod / Deployment / StatefulSet の連携は使わない）。

### 待ち時間と実行時間の上限

| 上限 | 値 | 数える範囲 |
|---|---|---|
| ワークフロー（`activeDeadlineSeconds`） | 6 時間 | Kueue での待ち + 変換 + 検証 |
| RayJob（`activeDeadlineSeconds`） | 30 分 | 変換の実行だけ。KubeRay は suspend が外れた時点で開始時刻（`status.startTime`）を付け直すので、待ち時間は含まれない |
| Kueue（`waitForPodsReady.timeout`） | 10 分 | 受け入れてから Ray クラスタが Ready になるまで。超えたら並び直し（3 回まで） |

Argo は RayJob の状態を見張るだけで、`Suspended`（待ち）は成功にも失敗にも当たらないので、待っている間は失敗しません。

### 終わったジョブの Ray クラスタはすぐ消す

Kueue はジョブが終わった時点で枠を返し、すぐ次のジョブを受け入れます。一方、RayJob の `ttlSecondsAfterFinished` の間は Ray クラスタの Pod が残り、
ノードの CPU / メモリを使い続けます。最初は終了後の確認用に 120 秒残していましたが、確認中に「次に受け入れたジョブの worker が `Insufficient cpu` で
Pending」になりました（枠の計算とノードの実際の空きがずれる）。そのため `ttlSecondsAfterFinished: 0` にして、枠を返すのと同時にノードも空くようにしています。

### 同じセッションを取り合わないための仕組み

- **デバイスごとのロック**: ワークフローの mutex 名を `mcap-convert-<device-id>` にして、同じデバイスのワークフローは 1 つずつ動かす。
  別のデバイスは並んで動き、RayJob の順番は Kueue が決める。
- **デバイスごとの検証**: validate も `--device-id` で自分のデバイスだけを見る。
  全体を見ると、並行して動いている他のデバイスの書きかけのファイルを「マニフェストの無い残骸」と誤判定するため。
- device-id を空にした「全デバイス」のワークフローは、デバイス指定のワークフローと同時に投げない（ロック名が別になるので取り合いになる）。
