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
│ docker buildx build     │ push   │ ghcr.io/<user>/mini-mlops-converter:0.1.0│
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
