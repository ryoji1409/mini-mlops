# フェーズ 1: S3 互換ストレージ（SeaweedFS）と MCAP のアップロード

SeaweedFS を入れて S3 API を使えるようにし、合成 MCAP を生成・検証して raw バケットにアップロードします。
構成の説明は [platform/README.md](../../platform/README.md)、生成器は [apps/mcap-generator/README.md](../../apps/mcap-generator/README.md)。

**完了条件**: raw バケットに MCAP と metadata.json がパーティション通りに並ぶ。

## 前提

- クラスタに Longhorn が入っていて、StorageClass `longhorn` が default になっている（`kubectl get storageclass`）
- `uv` / AWS CLI v2 / mcap CLI / `envsubst`（gettext）がある
- コマンドはすべてリポジトリのルートで実行する

## 1. `.env` を作る

S3 の接続先とアクセスキーをまとめたファイルです（`.gitignore` 済み。**コミットしない**）。

```bash
cp .env.example .env
chmod 600 .env
```

アクセスキーを作り、`.env` の `AWS_ACCESS_KEY_ID` などの空欄にエディタで書き込みます（2 つとも別の値にする）。

```bash
openssl rand -hex 10 | tr 'a-f' 'A-F'    # アクセスキー（20 文字）→ AWS_ACCESS_KEY_ID
openssl rand -hex 20                     # シークレットキー（40 文字）→ AWS_SECRET_ACCESS_KEY
```

| ユーザー | `.env` の変数 | 権限 | 使う場所 |
|---|---|---|---|
| admin | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 全権限 | ホストからの管理・アップロード |

`.env` を後から作り直すと、SeaweedFS に登録したキーと食い違います（キーを変えたら手順 2 からやり直す）。
`.env.example` に変数が増えたときは、増えた行だけを `.env` に書き足します。

## 2. SeaweedFS の S3 認証設定（Secret）を作る

SeaweedFS は、Secret `storage/seaweedfs-s3-config` の JSON からユーザーとキーを読みます。
雛形 [platform/templates/seaweedfs-s3-config.json](../../platform/templates/seaweedfs-s3-config.json) の `${…}` を `.env` の値で埋めて Secret にします
（後のフェーズで、`Read:raw` のように「操作:バケット名」と書いて権限を絞ったユーザーを足せる）。

```bash
set -a; source .env; set +a              # .env の変数をこのシェルに読み込む（以降の手順でも使う）

kubectl create namespace storage --dry-run=client -o yaml | kubectl apply -f -
envsubst '$AWS_ACCESS_KEY_ID $AWS_SECRET_ACCESS_KEY' \
  < platform/templates/seaweedfs-s3-config.json \
  | kubectl -n storage create secret generic seaweedfs-s3-config \
      --from-file=seaweedfs_s3_config=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f -
```

`--dry-run=client -o yaml | kubectl apply` にしているので、既に Secret があっても更新されます。
**SeaweedFS が動いた後に Secret を変えたときは、`kubectl -n storage rollout restart deploy/seaweedfs-all-in-one` で再起動しないと反映されません。**

## 3. SeaweedFS を入れる

```bash
helmfile -f platform/helmfile.yaml -l name=seaweedfs diff
helmfile -f platform/helmfile.yaml -l name=seaweedfs apply
kubectl -n storage rollout status deploy/seaweedfs-all-in-one --timeout=5m
kubectl -n storage get pod,pvc
```

期待される出力:

```
NAME                                       READY   STATUS    RESTARTS   AGE
pod/seaweedfs-all-in-one-xxxxxxxxxx-xxxxx  1/1     Running   0          1m

NAME                                              STATUS   CAPACITY   STORAGECLASS
persistentvolumeclaim/seaweedfs-all-in-one-data   Bound    5Gi        longhorn
```

## 4. ホストから S3 API に接続する

別のターミナルで動かしたままにします（Ctrl-C で終了）。`.env` の `AWS_ENDPOINT_URL=http://localhost:8333` がここを指します。

```bash
kubectl -n storage port-forward svc/seaweedfs-all-in-one 8333:8333
```

元のターミナルで、認証が通ることを確かめます。

```bash
aws s3 ls        # エラーなく終われば OK（バケットを作る前は何も出ない）
```

## 5. バケットを作る

```bash
for b in raw derived curated mlflow vectors; do
  aws s3api head-bucket --bucket "$b" 2>/dev/null && echo "既にあります: $b" || aws s3api create-bucket --bucket "$b"
done
aws s3 ls
```

| バケット | 中身 |
|---|---|
| `raw` | 生の MCAP と metadata.json（正本。変更しない） |
| `derived` | 変換後のデータ（Parquet、フレームのシャード） |
| `curated` | 学習用に整えたデータセット |
| `mlflow` | MLflow の artifact |
| `vectors` | LanceDB のテーブル |

`AWS_REGION` を `us-east-1` 以外にした場合は、`create-bucket` に `--create-bucket-configuration LocationConstraint=$AWS_REGION` が必要です（S3 の仕様）。

## 6. MCAP を生成する

2 デバイス × 3 セッション = 6 本を `output/` に作ります（`.gitignore` 済み）。同じ `--seed` なら同じファイルになります。

```bash
(cd apps/mcap-generator && uv run mcap-generate --devices 2 --sessions-per-device 3 --out ../../output --seed 42)
```

期待される出力（1 本目）:

```
[1/6] dev-001-20261104T070847Z dusk/rain 大0 小3 33s 2.8MB (/camera/front/image_compressed=165, /imu=3300, /gnss=330) 0.7s
```

## 7. 検証する

[scripts/validate-mcap.sh](../../scripts/README.md) が、セッションごとに sha256・サイズ・`mcap doctor`・`mcap info` と metadata.json の一致を確かめます。

```bash
scripts/validate-mcap.sh output
```

最後に `OK=6 NG=0` と出れば OK です。

## 8. アップロードする

[scripts/upload-raw.sh](../../scripts/README.md) が、検証を通ったセッションだけを `s3://raw/device=…/date=…/session_id=…/` にアップロードします。
同じ sha256 のものはスキップし、中身の違うものは上書きしません（raw は正本なので）。

```bash
scripts/upload-raw.sh --dry-run output   # 予定だけ表示
scripts/upload-raw.sh output
scripts/upload-raw.sh output             # 2 回目: すべて「スキップ（同じ sha256 がアップロード済み）」になる
```

## 9. raw バケットを確認する（完了条件）

```bash
aws s3 ls s3://raw/ --recursive
aws s3 ls s3://raw/ --recursive | grep -c '\.mcap$'              # 6
aws s3 ls s3://raw/ --recursive | grep -c '/metadata\.json$'     # 6
```

期待される出力（日時は実行時刻）:

```
... 1336 device=dev-001/date=2026-11-04/session_id=dev-001-20261104T013238Z/metadata.json
... 2638057 device=dev-001/date=2026-11-04/session_id=dev-001-20261104T013238Z/dev-001-20261104T013238Z.mcap
...（計 12 行 = 6 セッション × 2 ファイル。すべて device=…/date=YYYY-MM-DD/session_id=…/ の形）
```

1 本ダウンロードして、アップロード前と同じことを確かめます。

```bash
CHECK_DIR=$(mktemp -d)
aws s3 cp s3://raw/device=dev-001/date=2026-11-04/session_id=dev-001-20261104T013238Z/ "$CHECK_DIR/" --recursive
scripts/validate-mcap.sh "$CHECK_DIR"                           # OK=1 NG=0
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| Pod が `ContainerCreating` のまま、`describe` に `secret "seaweedfs-s3-config" not found` | 手順 2 の前に SeaweedFS を入れた。手順 2 を実行すれば起動が進む |
| PVC が `Pending` | StorageClass `longhorn` が無い / Longhorn が不調。`kubectl get storageclass` と `kubectl -n longhorn-system get pod` を確認 |
| PVC は `Bound` だが Pod が `ContainerCreating`、`describe` に `FailedAttachVolume ... volume is currently in detached state with some un-schedulable replicas` | PVC が大きすぎて、どのノードの Longhorn ディスクにもレプリカを置けない（下の「PVC が置けないとき」） |
| `envsubst: command not found` | gettext を入れる（`envsubst` は gettext に含まれる） |
| `Could not connect to the endpoint URL: "http://localhost:8333/..."` | 手順 4 の port-forward が動いていない |
| `InvalidAccessKeyId` / `SignatureDoesNotMatch` | `.env` のキーと Secret の中身が違う。手順 2 をやり直し、`kubectl -n storage rollout restart deploy/seaweedfs-all-in-one` |
| アップロードで `XAmzContentSHA256Mismatch` や checksum 関連のエラー | 新しい aws CLI が付ける CRC 系チェックサムに S3 互換側が未対応。`.env` に `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` と `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` があるか確認 |
| アップロードで `No free volumes left` / `failed to assign` | SeaweedFS の volume の上限に達した。platform/README.md の「よくあるエラー」 |
| `既に別の内容のオブジェクトがあります（上書きしません）` | 同じセッション ID で中身の違うファイルを上げようとした（seed や生成器の版を変えて再生成した等）。本当に置き換えるなら、内容を確認してから該当オブジェクトを手動で削除する |
| `検証 NG のためアップロードしません` | `scripts/validate-mcap.sh <そのディレクトリ>` で詳細を確認。途中で切れたファイルなら再生成する |

### PVC が置けないとき（`insufficient storage`）

理由を確かめます。

```bash
V=$(kubectl -n storage get pvc seaweedfs-all-in-one-data -o jsonpath='{.spec.volumeName}')
kubectl -n longhorn-system get volumes.longhorn.io "$V" \
  -o jsonpath='{range .status.conditions[*]}{.type}{"\t"}{.status}{"\t"}{.reason}{"\t"}{.message}{"\n"}{end}'
# → Scheduled  False  ReplicaSchedulingFailure  precheck new replica failed: insufficient storage

# 各ノードのディスク（バイト）: 最大 / 空き / 割り当て済み / 予約
kubectl -n longhorn-system get nodes.longhorn.io -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.diskStatus.*}max={.storageMaximum} avail={.storageAvailable} scheduled={.storageScheduled}{end}{"\t"}{range .spec.disks.*}reserved={.storageReserved}{end}{"\n"}{end}'
```

Longhorn は、レプリカを置くディスクごとに次の 2 つを確かめます（既定値の場合）。

- 割り当て済み + PVC のサイズ ≦ 最大 − 予約（予約は既定でディスクの 30%）
- 空き − PVC のサイズ ≧ 最大の 25%

両方を満たすノードがレプリカ数（2）以上ないと置けません。例えば最大 25GB・空き 14GB のノードでは、置けるのは約 7GB までです。
`platform/values/seaweedfs.yaml` の `allInOne.data.size` をこれより小さくし、**PVC を作り直します**（PVC は小さくできないため）。

**PVC を消すと中のデータも消えます。** アタッチできていなければデータは書かれていませんが、`get` で対象を確かめてから実行します。

```bash
kubectl -n storage get pvc,pod                                     # 消すものを確認
kubectl -n storage scale deploy/seaweedfs-all-in-one --replicas=0   # PVC を使っている Pod を止める（止めないと削除が終わらない）
kubectl -n storage delete pvc seaweedfs-all-in-one-data
helmfile -f platform/helmfile.yaml -l name=seaweedfs diff           # PVC のサイズと replicas: 1 が差分に出る
helmfile -f platform/helmfile.yaml -l name=seaweedfs apply          # PVC を新しいサイズで作り直し、Pod も 1 つに戻る
kubectl -n storage rollout status deploy/seaweedfs-all-in-one --timeout=5m
```
