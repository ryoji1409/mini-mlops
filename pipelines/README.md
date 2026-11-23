# pipelines — Argo WorkflowTemplate

## これは何か

パイプラインの各工程を Argo Workflows の WorkflowTemplate として定義します。
ワークフローとその中の RayJob は Namespace `mlops` で動きます。

| テンプレート | 内容 | フェーズ |
|---|---|---|
| `mcap-convert` | raw の MCAP を Ray Data で derived に変換し、DuckDB で検証する | 2 |

### mcap-convert の流れ

```
discover ──(未処理 > 0)──▶ convert（RayJob を作って待つ）──▶ validate
   │                          │                                  │
   │ converter discover       │ Ray head 1 + worker 2 を起動      │ converter validate
   │ 未処理の件数を出力        │ entrypoint: converter convert     │ DuckDB で行数照合
   └─(未処理 = 0)── convert をスキップ ───────────────────────────▶┘
```

| パラメータ | 既定 | 意味 |
|---|---|---|
| `converter-image` | `ghcr.io/OWNER/mini-mlops-converter:<version>`（登録時に OWNER を置き換える。タグは pyproject.toml の version と揃える） | 変換器のイメージ |
| `force` | `false` | `true` で変換済み・隔離済みのセッションも作り直す |
| `fail-on-quarantine` | `false` | `true` で隔離があればワークフローを失敗にする |

- **RayJob の成否**: `successCondition: status.jobStatus == SUCCEEDED`、`failureCondition: status.jobDeploymentStatus == Failed`
  （ジョブの失敗・期限切れ・submitter の失敗はどれも `jobDeploymentStatus` が `Failed` になる）。
- **後片付け**: `shutdownAfterJobFinishes: true` + `ttlSecondsAfterFinished: 120` で、ジョブ終了の 2 分後に Ray クラスタが消える。
  RayJob 自体は ownerReference でワークフローに紐づき、ワークフローが消える（7 日後）と一緒に消える。
- **同時実行**: `synchronization.mutexes` で 1 つずつ実行（同じセッションの取り合いを防ぐ。フェーズ 3 で Kueue に置き換える予定）。
- **ログ**: 各ステップのログは `s3://argo-artifacts/mlops/<ワークフロー名>/<Pod 名>/` に保存（Pod が消えても UI から見られる）。
  RayJob のジョブログは submitter Pod（`kubectl -n mlops logs job/<RayJob 名>`）で見る。

### リソース（limits）

| 対象 | limit | 備考 |
|---|---|---|
| Ray head | 1 CPU / 3Gi | `num-cpus=0`（タスクは載せない）、object store 200MB。Ray 2.59 の head は Dashboard の子プロセスなどで約 2.3GB 使う |
| Ray worker × 2 | 1 CPU / 1.25Gi | object store 250MB。変換時の実測ピークは約 0.6GB |
| submitter Pod | 0.5 CPU / 512Mi | `ray job submit` してログを追うだけ |
| **Ray 合計** | **6Gi** | 予算 6GB |
| discover / validate | 1 CPU / 512Mi | 短時間だけ動く。init / wait コンテナは別に 256Mi（Argo の executor 設定） |

## 実行手順

登録・実行・Ray Dashboard・隔離の確認の手順は [docs/setup/02-pipeline.md](../docs/setup/02-pipeline.md)（手順 7〜10）にあります。
よく使うコマンドだけ抜き出すと:

```bash
# 登録（OWNER を GitHub のユーザー名に置き換える。.env を読み込んだシェルで）
sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" pipelines/mcap-convert.yaml | kubectl apply -f -

# 実行して完了まで見守る
argo submit -n mlops --from workflowtemplate/mcap-convert --watch
argo submit -n mlops --from workflowtemplate/mcap-convert -p force=true --watch   # 作り直し
```

## 動作確認コマンド

```bash
argo get -n mlops @latest
argo logs -n mlops @latest | grep -E "変換対象|OK |QUARANTINED|MISMATCH|MISSING|検証"
kubectl -n mlops get workflow,rayjob,raycluster,pod
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
| ワークフローが `Pending` のまま、`waiting for lock` | 別の mcap-convert が動いている（mutex）。`argo list -n mlops` で確認 |
| discover が `AccessDenied` / `InvalidAccessKeyId` | Secret `pipeline-s3-credentials` のキーが SeaweedFS に登録されていない。[01-storage.md](../docs/setup/01-storage.md) 手順 2 をやり直して SeaweedFS を再起動 |
| convert が `forbidden: User "system:serviceaccount:mlops:mcap-pipeline" cannot create resource "rayjobs"` | Role が無い。`kubectl apply -k platform/manifests/pipelines` |
| convert が `cannot set blockOwnerDeletion if an ownerReference refers to a resource you can't set finalizers on` | API サーバで OwnerReferencesPermissionEnforcement が有効な環境。Role に `argoproj.io` の `workflows/finalizers` への `update` を足す |
| RayJob の head Pod が `OOMKilled` | head の limit（3Gi）を超えた。ドライバのメモリが増えた可能性。`kubectl -n mlops describe pod <head>` で確認し、メモリ予算の範囲で limit を上げる |
| RayJob が `Pending` のまま（head / worker が `Pending`） | ノードのメモリが足りず配置できない（head は request 2.5Gi）。`kubectl describe pod` の Events を確認し、他の負荷を減らす |
| validate が `ORPHAN` / `MISMATCH` で失敗 | apps/converter/README.md の「よくあるエラー」を参照。`argo submit ... -p force=true` で作り直せる |
| `argo logs` でログが出ない | Pod が GC 済み（成功 10 分後）。UI ではアーティファクト（s3://argo-artifacts）に保存されたログが見られる |
