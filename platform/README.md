# platform — 基盤部品（Helmfile）

## これは何か

クラスタに入れる基盤部品を Helmfile でまとめて管理します。

| リリース | Namespace | チャート | 用途 | フェーズ |
|---|---|---|---|---|
| `seaweedfs` | `storage` | `seaweedfs/seaweedfs` 4.52.0 | S3 互換オブジェクトストレージ（S3 API: ポート 8333） | 1 |

### SeaweedFS の構成（`values/seaweedfs.yaml`）

- **allInOne モード**: master / volume / filer / S3 ゲートウェイを 1 つの Pod（`deploy/seaweedfs-all-in-one`）で動かす。単一レプリカ。
- **リソース**: requests 250m / 512Mi、limits 1 CPU / 1Gi（予算 1.5GB 以内）。`GOMEMLIMIT=900MiB` で Go のメモリ使用を抑える。
- **データ**: Longhorn の PVC 5Gi（`seaweedfs-all-in-one-data`）。
  volume 1 個の上限 64MB × 最大 64 個 = 4GiB（後述の「よくあるエラー」参照）。
- **S3 認証**: 有効。アクセスキーは Secret `storage/seaweedfs-s3-config` から読む（雛形 `templates/seaweedfs-s3-config.json` から作る。[01-storage.md](../docs/setup/01-storage.md) 手順 2）。
- **公開方法**: ClusterIP。ホストからは `kubectl -n storage port-forward svc/seaweedfs-all-in-one 8333:8333` で `localhost:8333` に転送する。
  NodePort にしたい場合は values の `allInOne.service` のコメントを参照。

### S3 のユーザー（`templates/seaweedfs-s3-config.json`）

| ユーザー | キー（.env） | 権限 | 使う場所 |
|---|---|---|---|
| `admin` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 全権限 | ホストからの管理・アップロード |

## 実行手順

手順書に沿って、フェーズ順に入れます（リポジトリのルートで実行）。

| フェーズ | 手順書 | 入れるもの |
|---|---|---|
| 1 | [docs/setup/01-storage.md](../docs/setup/01-storage.md) | S3 認証の Secret、`seaweedfs` |

Helmfile は `-l name=<リリース名>` で 1 つずつ入れられます。`-l` なしの `helmfile -f platform/helmfile.yaml apply` は全リリースを入れます。

```bash
helmfile -f platform/helmfile.yaml diff     # 差分の確認（helm-diff プラグインが必要）
helmfile -f platform/helmfile.yaml apply
```

## 動作確認コマンド

```bash
helmfile -f platform/helmfile.yaml list                     # リリースと固定したチャートの版
kubectl -n storage get pod,pvc
```

期待される出力（抜粋）:

```
pod/seaweedfs-all-in-one-xxxxxxxxxx-xxxxx  1/1  Running
persistentvolumeclaim/seaweedfs-all-in-one-data   Bound   5Gi    longhorn
```

リソースの実使用量（metrics-server がある場合）: `kubectl top pod -A`

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| Pod が `ContainerCreating` のまま、`describe` に `secret "seaweedfs-s3-config" not found` | Secret を作る前にインストールした。[01-storage.md](../docs/setup/01-storage.md) 手順 2 を実行すれば自動で起動が進む |
| PVC が `Pending` | StorageClass `longhorn` が無い / Longhorn が不調。`kubectl get sc` を確認。別の StorageClass を使うなら values の `allInOne.data.storageClass` を変更 |
| `aws s3 ...` で `InvalidAccessKeyId` / `SignatureDoesNotMatch` | `.env` のキーと Secret の中身が食い違っている。[01-storage.md](../docs/setup/01-storage.md) 手順 2 をやり直し、`kubectl -n storage rollout restart deploy/seaweedfs-all-in-one` で再起動 |
| アップロードで `No free volumes left` / `failed to assign` | SeaweedFS はバケット（collection）ごとに volume を確保するため、上限に達した。values の `master.volumeSizeLimitMB` と `volume.dataDirs[0].maxVolumes` を調整（積が PVC サイズを超えないように）。既定の 1000MB のままだと 5Gi ではすぐ足りなくなるので 64MB にしている |
| Pod が `OOMKilled` | limit 1Gi を超えた。`kubectl -n storage top pod` で確認し、予算の範囲で limit と `GOMEMLIMIT` を上げる |
| `helmfile apply` で `Error: no matches for kind ...` などチャート取得のエラー | `helm repo update` 後に再実行。チャート版は `helmfile.yaml` で固定しているので、勝手に上がることはない |
