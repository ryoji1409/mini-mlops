# 構成とデータの流れ

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

