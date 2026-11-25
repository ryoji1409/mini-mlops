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

## フェーズ 4 時点（分散学習と MLflow）

```
┌──────────────────────────────────────── Kubernetes ─────────────────────────────────────────┐
│ ns mlops:  Workflow train-scene-classifier（SA train-pipeline）                               │
│   ① build-dataset: batch/v1 Job（data-processing キュー）── derived + raw → s3://curated/scene-cls/v<N>/ │
│   ② engine=trainjob: TrainJob ──▶ ns training                                               │
│      engine=rayjob : RayJob   ──▶ ns training     （どちらも training キュー経由）           │
│   ③ register: 正解率 ≥ 閾値なら MLflow レジストリに scene-classifier@candidate                 │
│                                                                                             │
│ ns kubeflow-system: kubeflow-trainer-controller-manager、jobset-controller                    │
│ ns training:                                                                                │
│   TrainJob ─▶ JobSet ─▶ Job <名前>-node-0 ─▶ Pod × ワーカー数（torchrun、DDP / gloo）       │
│   RayJob   ─▶ RayCluster（head + worker × ワーカー数）─▶ Ray Train（TorchTrainer、DDP / gloo）│
│        │ rank 0 だけ                       │ 全 rank                                         │
│        ▼ HTTP                              ▼ S3（training ユーザー）                         │
│ ns mlflow: MLflow（SQLite on Longhorn）     s3://curated/scene-cls/v<N>/（読む）               │
│        │ S3（mlflow ユーザー）             s3://curated/checkpoints/<ジョブ名>/（書く）         │
│        ▼                                                                                    │
│   s3://mlflow（モデル・画像）                                                                │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 系譜（どのモデルが、どのデータから作られたか）

```
MLflow モデル scene-classifier@candidate（version のタグ: dataset.*, trainer.image, converter.versions, run_id）
  └ run（パラメータ・メトリクス・混同行列・モデル。タグ dataset.manifest / dataset.hash）
      └ s3://curated/scene-cls/v<N>/manifest.json（inputs: セッションごとの derived マニフェストのパスと sha256、dataset_hash）
          └ s3://derived/_manifests/<session_id>.json（変換器の版、input.mcap のパスと sha256）
              └ s3://raw/device=…/date=…/session_id=…/<id>.mcap
```

各段に「次の段のパス」と「その中身のハッシュ」を書いておくので、パスをたどるだけでなく、途中で中身が差し替わっていないかも照合できます
（`trainer trace`、docs/setup/04-training.md 手順 11）。

### 学習の設計判断

#### 学習用と検証用はセッション単位で分ける

同じセッションの連続したフレームはほとんど同じ見た目なので、フレーム単位でランダムに分けると、精度が不当に高く出ます。
セッションごとに丸ごとどちらかに入れ、（時間帯, 天候）の 6 通りの組ごとに 2 セッションを検証用にします（検証用に全クラスが入るように）。
そのため、確認用のデータを 86 セッション（組ごとに約 14 セッション）まで増やしました。

#### 2 つの起動方式（TrainJob と RayJob）の使い分け

| 観点 | TrainJob（Kubeflow Trainer + torchrun） | RayJob（KubeRay + Ray Train） |
|---|---|---|
| Pod | ワーカーだけ（2 台で requests 1 CPU / 3Gi） | head + ワーカー + submitter（2 台で requests 1.6 CPU / 約 5.8Gi） |
| Kueue | training キューの自分の枠に収まる | メモリが training の枠（4.5Gi）では足りず、data-processing の空き枠を借りて動く |
| データの読み込み | 各 rank が担当のファイルを丸ごと読んでメモリに置く | Ray Data が流しながら配る（大きなデータに向く） |
| 失敗したとき | JobSet が全ワーカーを作り直し、学習コードがチェックポイントから再開（`failurePolicy.maxRestarts: 2`） | Ray Train がワーカーを作り直し、自分が保存したチェックポイントから再開（`FailureConfig.max_failures: 2`） |
| 向いている場面 | PyTorch のコードをそのまま Kubernetes で動かしたい。軽い | 変換（Ray Data）と前処理・学習を Ray でまとめたい。データが大きい |

どちらも同じ学習のコード（モデル・1 エポックの学習・評価・MLflow への記録）を使い、起動の部分（`torchrun_train.py` / `ray_train.py`）だけが違います。

**ワーカーの Pod を消したときの実測**（10 エポック、ワーカー 2 台、3 エポック目の保存直後に消した）:

| | TrainJob（JobSet） | RayJob（Ray Train） |
|---|---|---|
| 作り直される Pod | Job ごと全部（2 台とも新しい Pod）。`status.restarts` が 1 に | 消した Pod だけ（KubeRay が worker を 1 台作り直す。約 20 秒） |
| 学習のプロセス | 新しい Pod で torchrun からやり直し | 残った Pod でも学習ワーカーのプロセスを作り直す（Ray Train が全ワーカーを再起動） |
| 再開する場所 | 学習コードが `s3://curated/checkpoints/<名前>/epoch-0003.pt` を読む | Ray Train が自分で保存したチェックポイント（`…/checkpoints/ray/<名前>/checkpoint_…`）を渡す |
| MLflow | 同じ run に記録を続ける（チェックポイントに run_id） | 同じ |
| 結果 | 10 エポック完走 → 登録まで成功 | 同じ |

どちらも「1 台が落ちたら全員で最後のチェックポイントから」という動きは同じです。違うのは、作り直しを Kubernetes 側（JobSet）がやるか、
Ray の中（Ray Train）がやるかで、RayJob は消えた Pod だけを作り直すぶん、Pod の起動を待つ時間が短く済みます。

**変換と同時に投げたときの実測**（変換 2 本を先に投げ、60 秒後に学習を投げた）:

| | TrainJob 版 | RayJob 版 |
|---|---|---|
| データセット作成（data-processing キューの batch Job） | 変換 2 本の間は WaitingForQuota（Cohort のメモリ 10Gi を超える）。1 本終わると受け入れ | 同じ |
| 学習 | training の自分の枠（1 CPU / 3Gi）に収まり、変換 1 本と並んですぐ動いた | 1.6 CPU / 5888Mi。変換 1 本（4864Mi）が動いている間は Cohort の残り 5376Mi に入らず WaitingForQuota。変換が終わると、data-processing から 1280Mi を借りて受け入れ |

学習のパイプラインは、学習の前に「データセット作成」でも変換と同じ枠を取り合います。
RayJob 版は head の分だけ重く、変換が 1 本でも動いていると始められませんでした。

**ワーカー 1 台と 2 台の比較（実測）**（同じデータセット v2、バッチ 64、1 エポック = 学習用 15890 フレーム。`epoch_train_seconds` の平均）:

| 方式 | 1 台 | 2 台 | 速さの比 |
|---|---|---|---|
| TrainJob（torchrun） | 110.5 秒 | 53.0 秒（他の実行も 54〜56 秒） | 約 2.1 倍 |
| RayJob（Ray Train） | 102.8 秒 | 60.1 秒（他の実行も 59〜64 秒） | 約 1.7 倍 |

- torchrun 版は、ほぼ台数どおりに速くなりました。1 バッチあたりの時間は 1 台で約 0.45 秒、2 台で約 0.43 秒と変わらず、
  勾配の通信（all-reduce、gloo）のコストはほぼ見えません。モデルが小さく（約 7 万パラメータ = 勾配は約 280KB）、
  CPU 1 コアでの計算（160x90 の画像 64 枚の順伝播・逆伝播）のほうがずっと重いためです。モデルが大きくなる・計算が速くなる
  （GPU にする）と、通信の割合が増えて、台数ほどには速くならなくなります。
- RayJob 版は、1 台では torchrun 版より速く（Ray Data が次のバッチの読み込みを学習と並行して進めるため）、2 台では遅くなりました
  （1.7 倍止まり）。2 台にすると、データを 2 つに分けて配る調整（split）や、別の Pod で読んだデータの受け渡しが増えるためと考えられます。
- この大きさのデータとモデルでは、2 台にする効果は十分ありますが、Ray Data の便利さ（流しながら配る）はまだ活きていません。
  データが大きく、読み込み・前処理が重いときに差が出るはずです。

#### Ray のワーカーは「CPU 2 つ」と名乗る

Ray Train の学習ワーカーは CPU を 1 つ予約します。Pod の CPU limit（1）と同じ数を Ray に名乗らせると、学習ワーカーが CPU を占有し、
Ray Data の読み込みタスクが動けずに止まります。そこで worker は `num-cpus: "2"` で起動し（実際の limit は 1 のまま）、
1 つを学習、1 つを読み込みに使います。

ただし「CPU 2 つ」と名乗らせると、Ray からは 1 つの Pod に学習ワーカーが 2 つ入るように見えます。Ray Train は既定でワーカーを
なるべく同じ場所にまとめる（PACK）ので、最初の実行では rank 0 と 1 が同じ Pod に入り、Pod のメモリ上限（1.75GB）の 95% を超えて
Ray のメモリ監視に学習ワーカーが強制終了されました（もう 1 台の Pod は学習に使われていなかった）。
`ScalingConfig(placement_strategy="STRICT_SPREAD")` で、学習ワーカーを必ず別々の Pod に置くようにしています。

#### MLflow はサーバー経由で成果物を保存する

MLflow は `--artifacts-destination s3://mlflow` で起動し、成果物の読み書きをサーバーが代わりに行います（プロキシ方式）。
学習の Pod は HTTP の API を呼ぶだけで、`s3://mlflow` のキーを持ちません。学習の Pod が持つのは、データセットとチェックポイントのための
`curated` のキー（training ユーザー）だけです。チェックポイントは `s3://mlflow` ではなく `s3://curated/checkpoints/` に置き、
MLflow には最終のモデルだけを残します。

- バックエンドは SQLite（Longhorn の PVC 1Gi）。SQLite は同時書き込みに弱いので、レプリカ 1・ワーカー 1・更新方式 Recreate
- MLflow 3.x が既定で起動する裏の仕事用のプロセス（GenAI 向けの定期タスク。9 プロセス、合計 2GB 超）は使わないので止める
  （`MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`。起動したままだと limit 1Gi で OOMKilled になった）
- MLflow 3.5 以降は、知らないホスト名のリクエストを拒否する（`--allowed-hosts`）。クラスタ内の Service 名と localhost だけを許可
- 画面で画像を開くとき、MLflow はまず S3 の署名付き URL で直接取ろうとし（ブラウザからクラスタ内の S3 には届かない）、失敗するとサーバー経由で取り直す

#### リソースとクォータ

| 部品 | requests / limits | 予算 |
|---|---|---|
| 学習ワーカー（1 台） | requests 500m / 1536Mi、limit 1 CPU / 1792Mi | 「Kubeflow Trainer + 学習ジョブ 4GB」: ワーカー 2 台 limit 3.5Gi + コントローラ 2 つ 256Mi ずつ |
| Trainer / JobSet のコントローラ | 50m / 128Mi、limit 500m / 256Mi | 同上 |
| MLflow | 100m / 512Mi、limit 1 CPU / 1Gi | 「MLflow 1GB」 |
| データセット作成の Job | 500m / 768Mi、limit 1 CPU / 1536Mi | data-processing キューの枠の中 |

MLflow とコントローラで CPU が約 0.2 増えるので、ノードで実際に使える CPU は約 4.3 になります。クォータの合計（4 CPU）は、まだその内側です。

**合計だけでなく、1 台ごとの空きも見る**: 最初は学習ワーカーの requests を 1 CPU にしていましたが、各ノードの CPU の空きは 0.9 前後で、
1 コアまるごと空いているノードがありませんでした。Kueue は合計の枠しか見ないので受け入れはするものの、2 台目の Pod が載らず、
10 分ごとに Kueue が戻して受け入れ直すのを繰り返しました（waitForPodsReady）。requests を 500m に下げ（limit は 1 のまま）、
空きが細切れでも載るようにしています。
