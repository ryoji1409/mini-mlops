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

## フェーズ 5 時点（動画の索引・データの選別・高速な読み出し）

```
ホスト: mcap-generate --video ─▶ upload-raw.sh
                                   │
s3://raw/device=…/session_id=…/  <id>.mcap（チャンク = 1 GOP = 1 秒）、ground_truth.parquet（フレームごとの正解値）、metadata.json
   │ footer + summary だけ（Range GET 2 回）              │ フレームごとの正解値
   ▼                                                      ▼
┌──────────────── ns mlops: Workflow build-video-index（SA curate-pipeline、Job は data-processing キュー）─────────┐
│ ① build-index ─▶ s3://derived/index/gops/device=…/date=…/<session>.parquet   GOP ごとの MCAP 参照位置            │
│                  s3://derived/index/routes/device=…/date=…/<session>.parquet 予定ルート（ルート用の小さなチャンクだけ読む）│
│ ② tag (DuckDB) ─▶ s3://derived/tags/segments.parquet                         セグメントごとの集計とタグ          │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌──────────────── ns mlops: Workflow select-and-curate ───────────────────────────────────────────────────────────┐
│ ③ select（DuckDB + sql/*.sql、任意で外から渡した segment_id）─▶ s3://curated/<名前>/v<N>/manifest.json, shards/*.parquet │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
   │ サンプル一覧（segment_id + GOP の参照位置 + GOP 内のフレーム番号 + 正解値 + ルートの場所）
   ▼
学習器の VideoManifestDataset: GOP ごとに Range GET（チャンク 1 つ）→ H.265 をデコード（PyAV）→ 同じチャンクの GNSS で位置と向き
   → 経路ラスタ（navi-raster）を描く。先読みのスレッドで prefetch 個先の GOP まで並行に進める
```

### MCAP の作り方: 1 チャンク = 1 GOP

MCAP のメッセージはチャンクにまとめられ、チャンクごとに zstd で圧縮されます。
**圧縮されているので、チャンクの途中だけを読むことはできず、取り出せる最小の単位はチャンク 1 つ** です。
そこで生成器は、動画の GOP の先頭（IDR）を書く直前に作りかけのチャンクを閉じ（mcap の `Writer.flush()`）、1 チャンク = 1 GOP = 1 秒にしています。

- 索引は summary のチャンクの索引（ChunkIndex）から、そのまま「GOP の参照位置 = チャンクの開始オフセットと長さ」を取れる（チャンクの中身を読む必要がない）
- ローダーは 1 GOP につき Range GET 1 回。前後の GOP のデータを余分に取らない
- 動画は B フレームなし・GOP 長 10 で固定・各 IDR に VPS/SPS/PPS を付ける（x265 の `repeat-headers=1`）・Annex B・1 メッセージ = 1 フレーム。
  途中の GOP だけを取り出しても、その GOP の先頭に復号の設定が付いているので単独でデコードできる
- 予定ルートは記録の先頭に 1 回だけ出し、直後にチャンクを閉じて「ルートだけの小さなチャンク」にする（索引がルートを取るときに GOP を読まずに済む。約 1.4KB）

**代償（同じチャンクに他のトピックが同居する）**: 同じ 1 秒の JPEG・IMU・GNSS も同じチャンクに入るので、動画だけが欲しくてもそれらも一緒に読みます。
手元で測ったチャンクの中身（圧縮前、1 チャンクの平均 約 133KB、圧縮後 約 92KB）:

| トピック | 1 チャンクの平均 | 割合 |
|---|---|---|
| `/camera/front/image_compressed`（JPEG 640×360、5Hz） | 110KB | 82.8% |
| `/camera/front/compressed_video`（H.265 320×180、10Hz） | 12KB | 9.0% |
| `/imu`（100Hz） | 9.7KB | 7.3% |
| `/gnss`（10Hz） | 1.2KB | 0.9% |

つまり、このデータでは読んだバイト数の約 8 割が学習に使わない JPEG です。選択肢と比べると:

| 方式 | 1 GOP の読み出し | 良い点 | 悪い点 |
|---|---|---|---|
| A. 1 チャンク = 1 GOP（全トピック同居。今回） | チャンク 1 つ（他のトピック込み） | MCAP 1 つで完結。GNSS も同じ読み出しで取れる（向きの推定に使う） | 他のトピックが重いほど無駄が増える |
| B. 動画だけのチャンクを別に作る（同じ MCAP の中で、トピックごとにチャンクを分ける） | 動画のチャンクだけ（約 12KB） | 無駄がほぼない | 書き込み側でトピックごとにチャンクを管理する必要がある（mcap の標準の Writer は 1 つのチャンクに全部入れる）。GNSS は別の読み出しになる |
| C. 動画を別のファイル（MP4 など）にする | ファイルの一部（MP4 の索引から位置を引く） | 動画の道具（再生・編集）がそのまま使える | 正本が 2 つになり、時刻の対応と整合性を自分で管理する必要がある |

この環境では「MCAP を正本にして、索引で参照する」流れを確かめることを優先して A にしました。
読み出し量を詰めたくなったら、まず JPEG を MCAP から外すか（動画があれば要らない）、B に進みます。

### 索引（build-index）は footer と summary だけを読む

MCAP の末尾には footer（summary の開始位置）があり、summary にはチャンクの索引（開始位置・長さ・時刻の範囲・含まれるチャンネル）があります。
索引作成は Range GET を 2 回（末尾の 37 バイト → summary の開始位置からファイルの末尾まで）だけして、ファイルの本体は読みません。

- 60 秒・約 5.8MB の MCAP で、summary は約 12KB。確認用に 1 GOP を実際に読む分を足しても、MCAP 全体の約 1.9%
- mcap の `make_reader` にシークできるファイル（Range GET で読むもの）を渡す方法も試しましたが、先頭の magic を確かめる読み出しで
  バッファの大きさ（64KB）だけ本体を読んでしまうので、2 回の Range GET を自分で書く形にしました
- 同じ MCAP（metadata.json の sha256）・同じ索引の版なら作り直さない（冪等）

### セグメントの正解値の集計（tag）

正解値はフレームごと（生成時の真値）に raw の `ground_truth.parquet` に置き、集計はタグ付けの SQL（`sql/tag_segments.sql`）で行います。
フレームごとの値は残すので、集計の決まりを変えてもタグ付けをやり直すだけで済みます。

| 列 | 集計 | 使いどころ |
|---|---|---|
| `*_max`（最大値） | セグメント中で最も多いときの数 | 「2 個以上写る場面」のような、見落としたくない条件 |
| `*_mean`（平均値） | 写っていた時間の割合の目安 | 混み具合の度合い |
| `*_any`（1 回でも写ったか） | タグはこれで付ける | 「含むセグメント」を漏れなく拾う |
| `time_of_day` / `weather` | セッションの値をそのまま | セッションの中では変わらない |

### 経路ラスタの向き（heading）の決め方

- **GNSS の当てはめ**: 同じ GOP（= 同じチャンク）に入っている 1 秒分の GNSS（10 点）に直線を当てはめ、その傾きを進行方向にする。
  1 点ずつの差分は GNSS の誤差（約 0.5m）で大きくぶれるが、1 秒分をまとめると安定する。追加の読み出しも要らない
- **IMU は使わない**: 角速度の積分はずれ（ドリフト）がたまり、積分を始める状態も要る。サンプルを順不同に取り出すローダーでは前の状態を持てない
- **止まっているとき**: 1 秒の移動量が 1m 未満なら向きが決まらないので、予定ルート上の一番近い点での経路の向き（接線）を使う（サンプルに `heading_source` として記録）

経路ラスタは事前に画像として保存せず、サンプルごとにその場で描きます（128×128 px、0.5 m/px。描画は手元で 1 枚 約 0.05ms）。
描き方（解像度・範囲・線の太さ）を変えても、データを作り直さずに済むためです。

### 先読み（prefetch）

ローダーは GOP ごとに「Range GET → チャンクの展開 → デコード → 経路ラスタ」をスレッドで並行に進め、元の順番で返します。
S3 の 1 回の応答待ちが長いほど効果が大きく、手元で人工的な遅延 50ms を入れたときは、先読みなし 16.7 samples/sec → 先読み 4 で 59.4 samples/sec でした
（読み出しのバイト数・回数は変わらない）。デコーダは差し替えられるようにしてあり（`VideoDecoder`）、この環境の PyAV（CPU）の代わりに、
GPU のある環境では NVDEC（GPU の動画デコード専用の回路）を使う実装を入れる想定です。

### 選別とサンプル一覧（select）

- 選別の条件は SQL ファイル（`apps/indexer/src/indexer/sql/*.sql`）。SQL は `segment_id` を返すだけにして、サンプルの作り方（GOP との結び付け・フレームの選び方・シャード分け）は共通にする
- 1 サンプル = segment_id + GOP（MCAP の参照位置）+ GOP 内のフレーム番号 + 正解値。フレームの選び方は `first`（IDR だけ。デコードが一番軽い）/ `random`（sha256 で決まる。種が同じなら同じ）/ `all`
- サンプルは MCAP の参照位置の順に並べ、GOP の切れ目でシャードに分ける（1 つの GOP が 2 つのシャードにまたがらない → 同じ GOP を 2 回読まない）
- 外から渡した segment_id の一覧（`--segment-ids-uri`）を `candidates` という表で使える。フェーズ 6 の検索の結果を、タグの条件と組み合わせて選別に流すための入口
- 同じデータセット・バージョンに同じ中身があればスキップ、違えばエラー（フェーズ 4 のデータセットと同じ考え方）

### 新しく使った OSS のライセンス

| もの | ライセンス | 注意 |
|---|---|---|
| PyAV 19.0.2 | BSD-3-Clause | 配布されている wheel には FFmpeg と x264 / x265 が同梱されている |
| FFmpeg（PyAV に同梱） | LGPL-2.1 以降（GPL の部品と一緒に組み込むと全体が GPL） | 同梱の FFmpeg は x264 / x265 を含むので GPL として扱う |
| x265（PyAV に同梱） | GPL-2.0 以降（商用ライセンスもある） | 生成器のエンコードで使う。デコード（FFmpeg の hevc）は x265 を使わない |
| DuckDB 1.5.6 | MIT | httpfs 拡張はイメージ作成時に入れる |

GPL の部品を含むイメージ（学習器 0.2.0）は、このリポジトリの中で使うだけなら問題になりませんが、イメージを外に配布する場合は GPL の条件（ソースの提供など）を満たす必要があります。
エンコードが要らない環境（学習のローダー）では、x265 を含まない FFmpeg を自分でビルドした PyAV に替えることもできます。

### リソース

| 部品 | requests / limits | 予算 |
|---|---|---|
| 索引・タグ付け・選別の Job | 200m / 256Mi、limit 1 CPU / 768Mi | data-processing キューの枠の中 |

## フェーズ 6 時点（画像の埋め込みと LanceDB での検索）

```
ホスト（1 回だけ）: embedder register-model ─▶ Hugging Face（リビジョン固定）─▶ MLflow のモデルレジストリ（siglip-base-p16-224 版 N、エイリアス embedding）
                                                                              │ 重み（s3://mlflow、サーバー経由）
┌──────── ns mlops: Workflow video-index-and-embed（= build-video-index の手順 + embed-video の手順。Job は data-processing キュー）────────┐
│ ① build-index → ② tag（フェーズ 5 と同じ。作成済みならスキップ）                                                                        │
│ ③ embed: MLflow から重みを emptyDir へ → 索引のキーフレームごとにチャンクを Range GET → 先頭フレームだけデコード（PyAV）→ SigLIP       │
│          ─▶ s3://vectors/siglip-base-p16-224__7fd15f0__frames（1 行 = 1 キーフレーム）                                                │
│          ─▶ s3://vectors/siglip-base-p16-224__7fd15f0__segments（1 行 = 1 セグメント、キーフレームの平均ベクトル）                       │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
ホスト: embedder search --text "night" ─▶ s3://vectors/queries/<名前>.parquet（segment_id の一覧）+ PNG
   │ segment-ids-uri
   ▼
Workflow select-and-curate -p sql=candidates ─▶ s3://curated/<名前>/v<N>/（フェーズ 5 と同じ manifest とシャード）─▶ 学習のローダー
```

### 埋め込みはキーフレームだけで作る

- 各 GOP の先頭フレーム（IDR）だけを使います（1 秒に 1 枚、1 セグメントあたり 10 枚）。IDR は 1 枚だけでデコードできるので、デコードの手間が一番少ない
- ただし読み出しの単位はチャンク（= 1 GOP。画像の JPEG・IMU なども入っている）なので、全部の GOP を埋め込むと MCAP のほぼ全部を少しずつ読むことになります（手元で 97.7%）。
  `--gop-stride N`（N 秒に 1 枚）で間引くと、読み出しもほぼ 1/N になります（N = 5 で 19.7%）。フェーズ 5 の「代わりの方式（動画だけのチャンク）」にすれば、キーフレームだけの読み出しはさらに小さくなる
- セグメントのベクトルはキーフレームの平均（長さ 1 にそろえ直す）。セグメント全体の雰囲気（時間帯・天候）には平均が向き、
  一部の時間にだけ写るもの（近くの小物体）には、キーフレームごとに比べて一番近いものを使う方（`--level frames`）が向きます

### モデルの重みを MLflow のモデルレジストリに置く

- インターネット（Hugging Face）に出るのは、ホストで登録するときの 1 回だけ。リビジョンはコミットのハッシュで固定し、ファイルごとの sha256 を一緒に保存する
- ジョブはエイリアス（`embedding`）で版を引き、MLflow サーバー経由で emptyDir にダウンロードして sha256 を確かめてから使う。ジョブに `s3://mlflow` のキーは配らない（フェーズ 4 と同じ）
- モデルを替えるときは、新しい版を登録してエイリアスを付け替える。表の名前にモデル名とリビジョンが入るので、新しいモデルの表は別にでき、古い表と比べられる
- 代わりの案と比べた理由: 重みをイメージに入れると、イメージが 0.8GB 大きくなり、モデルを替えるたびにイメージを作り直すことになる。
  PVC のキャッシュは、初回にクラスタからインターネットに出る必要があり、どのリビジョンが入っているかが外から見えない

### 冪等性と同時実行

- セッションごとに「表に入っている GOP の参照位置（MCAP・GOP 番号・chunk_offset）の集合」と索引を比べ、同じならスキップする。
  索引を作り直して参照位置が変わったセッション、間引きの間隔を変えたセッションは、古い行を消して入れ直す。索引から消えたセッションの行も消す
- 書き込みはセッション単位なので、途中で止まっても、書き終えたセッションは次の実行でスキップされる
- LanceDB の表は、書く人が 1 人なら安全。Argo の mutex（`video-embed`）で、埋め込みのワークフローを同時に 1 本にしている
  （`video-index-and-embed` は `video-index` と `video-embed` の両方を取る）
- SeaweedFS 4.52 の上で、LanceDB の作成・追加・削除・検索・絞り込みが動くことを確かめた

### 評価（precision@k）

- 正解値（フェーズ 5 のタグ）と照らし合わせ、上位 10 件のうち正解の割合を出す。でたらめに選んだときの期待値（正解の割合）と並べる
- 時間帯・天候は画面全体の色・明るさ・雨の線で分かるので、ほぼ当たる。近くの小物体は、画像の中で小さく（224×224 に縮めるとさらに小さい）、
  でたらめとあまり変わらない。合成画像の描き方を変えれば上がる見込みはあるが、変えていない
- 物体のクエリは、描いているものをそのまま言葉にした（小物体 = 円柱、大物体 = 立方体）。精度が上がる言い回しを探して選ぶことはしていない

### 新しく使った OSS・モデルのライセンス

| もの | 開発元 | ライセンス | 注意 |
|---|---|---|---|
| SigLIP（`google/siglip-base-patch16-224`、リビジョン `7fd15f0`） | Google（米国） | Apache-2.0 | ほかのモデルからの派生ではない |
| transformers 5.22.2 | Hugging Face（米国） | Apache-2.0 | |
| sentencepiece 0.2.3 | Google | Apache-2.0 | SigLIP のトークナイザ |
| LanceDB 0.41.0 | LanceDB, Inc.（米国） | Apache-2.0 | |

埋め込みのイメージにも PyAV（GPL の x264 / x265 入りの FFmpeg）が入るので、学習器 0.2.0 と同じく、外に配布するときは GPL の条件を満たす必要があります。

### リソース

| 部品 | requests / limits | 予算 |
|---|---|---|
| 埋め込みの Job | 2 CPU / 1536Mi、limit 2 CPU / 2560Mi、重みの emptyDir 上限 2Gi | data-processing キューの枠（2 CPU / 5.5Gi）の中。実測で 1 フレーム約 0.23 秒、メモリ最大約 0.9GB |
