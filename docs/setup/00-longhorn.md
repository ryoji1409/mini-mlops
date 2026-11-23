# フェーズ 0: Longhorn（default StorageClass）

クラスタに分散ブロックストレージ Longhorn を入れ、StorageClass `longhorn` を default にします。
後のフェーズの PVC（SeaweedFS など）はすべてこれを使います。構成の説明は [platform/README.md](../../platform/README.md)。

**完了条件**: テスト用の PVC を作って Pod から読み書きでき、Longhorn UI でボリュームが Healthy。

## 前提

- `kubectl` の接続先がこのクラスタになっている（`kubectl get nodes` で全ノードが `Ready`）
- `helm` / `helmfile` がある（バージョンはルートの README の「必要なツール」）
- コマンドはすべてリポジトリのルートで実行する

## 1. 既に Longhorn が入っていないか確認する

```bash
kubectl -n longhorn-system get settings.longhorn.io current-longhorn-version -o jsonpath='{.value}{"\n"}'
helm -n longhorn-system list
kubectl get storageclass
```

| 結果 | どうするか |
|---|---|
| `NotFound` / 何も出ない | 入っていない。手順 2 へ |
| helm のリリース名 `longhorn` で、版が `v1.12.2` 以下 | Helmfile がそのリリースを引き継いで 1.12.2 に上げる。手順 4 の `diff` を必ず確認する |
| 版が `v1.12.2` より新しい | **先に進まない**（Longhorn は版を下げられない）。`platform/helmfile.yaml` の版を合わせる |
| helm 以外（`kubectl apply` のマニフェストなど）で入っている | **先に進まない**。Helm は引き継げないので、移行方法を先に決める |

`kubectl get storageclass` で `longhorn` 以外に `(default)` が付いたものがあれば、default を外しておきます
（`kubectl patch storageclass <名前> -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'`）。

## 2. ノードの準備を確認する（longhornctl）

Longhorn は各ノードに open-iscsi（`iscsid` が動いている）や NFSv4 クライアントなどを必要とします。
Longhorn 公式の CLI `longhornctl` で確認します。`longhornctl` は Namespace `longhorn-system` に一時的な DaemonSet を作って各ノードを調べ、
終わったら消します（この一時的な Pod には resources が付きません。数十秒で消える確認用の Pod です）。

```bash
# ホストの OS / CPU に合う longhornctl をダウンロードして、sha256 を照合する（.tools/ は .gitignore 済み）
LONGHORN_VERSION=v1.12.2
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')
mkdir -p .tools
curl -sSfL -o .tools/longhornctl-${OS}-${ARCH} https://github.com/longhorn/cli/releases/download/${LONGHORN_VERSION}/longhornctl-${OS}-${ARCH}
curl -sSfL -o .tools/longhornctl-${OS}-${ARCH}.sha256 https://github.com/longhorn/cli/releases/download/${LONGHORN_VERSION}/longhornctl-${OS}-${ARCH}.sha256
(cd .tools && shasum -a 256 -c longhornctl-${OS}-${ARCH}.sha256)
chmod +x .tools/longhornctl-${OS}-${ARCH}

# 確認する（ノードは変更しない）
kubectl create namespace longhorn-system --dry-run=client -o yaml | kubectl apply -f -
.tools/longhornctl-${OS}-${ARCH} check preflight --image longhornio/longhorn-cli:${LONGHORN_VERSION}
```

期待される出力（抜粋。ノードの数だけ出る）:

```
longhornctl-<os>-<arch>: OK                      ← sha256 の照合
...
node-1:
  info:
  - Service iscsid is running
  - NFS4 is supported
  - Package nfs-common is installed
  - Package open-iscsi is installed
```

`error` が出た場合は、足りないパッケージを全ノードに入れます。**ノードの OS にパッケージを入れる操作**（apt でのパッケージ一覧の更新を含む）なので、
内容を確認してから実行し、終わったらもう一度 `check preflight` を実行します。

```bash
.tools/longhornctl-${OS}-${ARCH} install preflight --image longhornio/longhorn-cli:${LONGHORN_VERSION}
```

## 3. multipathd の警告が出た場合

`multipathd.service is running` の警告が出たら、各ノードで Longhorn のデバイスを multipath の対象外にします。
Ubuntu Server などでは multipathd が既定で動いていて、Longhorn のデバイス（`/dev/sdX`）を掴んでマウントが
`already mounted or mount point busy` で失敗することがあるためです（Longhorn 公式 KB）。multipath を使っていないノードでは次で十分です。

```bash
# 各ノードで（ssh して）実行
sudo tee -a /etc/multipath.conf <<'EOF'
blacklist {
    devnode "^sd[a-z0-9]+"
}
EOF
sudo systemctl restart multipathd.service
sudo multipath -t | grep -A2 blacklist        # 設定が入ったか確認
```

## 4. Longhorn を入れる

```bash
helmfile -f platform/helmfile.yaml -l name=longhorn diff     # 差分を確認（helm-diff プラグインが必要）
helmfile -f platform/helmfile.yaml -l name=longhorn apply    # 起動を待つので数分かかる（最大 15 分）
```

CSI のコントローラは、Longhorn の driver-deployer があとから作ります。揃うまで待ちます。

```bash
kubectl -n longhorn-system rollout status ds/longhorn-manager --timeout=5m
kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer --timeout=5m
kubectl -n longhorn-system get deploy,ds          # csi-attacher / csi-provisioner / csi-resizer / csi-snapshotter が出るまで繰り返す
kubectl -n longhorn-system rollout status ds/longhorn-csi-plugin --timeout=5m
```

期待される出力（`kubectl -n longhorn-system get pod`、抜粋）:

```
NAME                                     READY   STATUS
longhorn-manager-xxxxx                   2/2     Running        ← ノードの数だけ
longhorn-csi-plugin-xxxxx                3/3     Running        ← ノードの数だけ
engine-image-ei-xxxxxxxx-xxxxx           1/1     Running        ← ノードの数だけ
instance-manager-xxxxxxxx                1/1     Running        ← ノードの数だけ
longhorn-driver-deployer-xxxxx           1/1     Running
longhorn-ui-xxxxx                        1/1     Running
csi-attacher-xxxxx / csi-provisioner-xxxxx / csi-resizer-xxxxx / csi-snapshotter-xxxxx   1/1 Running（各 1 つ）
```

## 5. StorageClass とノードを確認する

```bash
kubectl get storageclass
kubectl -n longhorn-system get nodes.longhorn.io
```

期待される出力:

```
NAME                 PROVISIONER          RECLAIMPOLICY   VOLUMEBINDINGMODE   ALLOWVOLUMEEXPANSION
longhorn (default)   driver.longhorn.io   Delete          Immediate           true      ← default は longhorn だけ

NAME     READY   ALLOWSCHEDULING   SCHEDULABLE
node-1   True    true              True                                                 ← 全ノードが True
```

## 6. テスト用の PVC で読み書きを確認する（完了条件）

`platform/manifests/longhorn-check/` は、Namespace `longhorn-check`・PVC 1Gi（StorageClass を書かない = default を使う）・
PVC を `/data` にマウントして待機する Pod です。

```bash
# PVC と Pod を作る
kubectl apply -k platform/manifests/longhorn-check
kubectl -n longhorn-check wait --for=jsonpath='{.status.phase}'=Bound pvc/check-data --timeout=5m
kubectl -n longhorn-check wait --for=condition=Ready pod/check-pod --timeout=5m

# ファイルを書く
kubectl -n longhorn-check exec check-pod -- sh -c 'date > /data/check.txt && sync && cat /data/check.txt'

# Pod を消して作り直し、同じ内容が読めること（データが Pod ではなくボリュームに残っている）を確かめる
kubectl -n longhorn-check delete pod check-pod
kubectl apply -k platform/manifests/longhorn-check
kubectl -n longhorn-check wait --for=condition=Ready pod/check-pod --timeout=5m
kubectl -n longhorn-check exec check-pod -- cat /data/check.txt      # 書いたときと同じ日時が出れば OK

# ボリュームの状態とレプリカの置き場所
VOLUME=$(kubectl -n longhorn-check get pvc check-data -o jsonpath='{.spec.volumeName}')
kubectl -n longhorn-system get volumes.longhorn.io "$VOLUME"
kubectl -n longhorn-system get replicas.longhorn.io -l longhornvolume="$VOLUME" \
  -o custom-columns=REPLICA:.metadata.name,NODE:.spec.nodeID,STATE:.status.currentState
```

期待される出力（最後の 2 つ）:

```
NAME                                       DATA ENGINE   STATE      ROBUSTNESS   ...
pvc-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   v1            attached   healthy      ← アタッチ直後は degraded のことがある。少し待って再確認

REPLICA                    NODE     STATE
pvc-xxxxxxxx-r-xxxxxxxx    node-2   running       ← レプリカ 2 つが別々のノードにある
pvc-xxxxxxxx-r-yyyyyyyy    node-4   running
```

## 7. Longhorn UI で確認する（完了条件）

UI には認証がありません（ボリュームの削除などもできる）。NodePort などで公開せず、port-forward でだけ開きます。

```bash
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8090:80     # Ctrl-C で終了
```

ブラウザで http://localhost:8090 → **Volume** で、手順 6 のボリュームが **Healthy** なら完了です。
**Node** 画面で各ノードのディスクの空きも確認しておきます（既定では空きが 25% を切ったノードにはレプリカを置かない）。

## 8. テスト用の PVC を片付ける

**PVC を消すと、Longhorn のボリュームとデータも消えます。** UI で確認が終わってから実行します。

```bash
kubectl -n longhorn-check get pod,pvc          # 消すものを確認
kubectl delete namespace longhorn-check
```

## メモリ使用量の確認

```bash
kubectl -n longhorn-system top pod             # metrics-server がある場合。合計がメモリ予算（2GB 程度）に収まっているか
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `shasum: ... FAILED` | ダウンロードが壊れている / 途中で切れた。`.tools/` のファイルを消してやり直す |
| `check preflight` で `Service iscsid is not running` / `Package open-iscsi is not installed` | ノードの準備が足りない。手順 2 の `install preflight` を実行 |
| `check preflight` で `multipathd.service is running` の警告 | 手順 3 を各ノードで行う |
| `helmfile apply` が `longhorn` で長く止まる / タイムアウト | Pod の起動待ち。`kubectl -n longhorn-system get pod` で `ImagePullBackOff` / `CrashLoopBackOff` を確認。manager のログは `kubectl -n longhorn-system logs ds/longhorn-manager -c longhorn-manager` |
| longhorn-manager が `CrashLoopBackOff`、ログに `iscsiadm` / `iscsid` のエラー | open-iscsi が入っていない / iscsid が止まっている。手順 2 |
| PVC は `Bound` だが Pod が `ContainerCreating`、`describe` に `already mounted or mount point busy` | multipathd がデバイスを掴んでいる。手順 3 を行い、Pod を作り直す |
| Pod が `ContainerCreating`、`describe` に `FailedAttachVolume ... some un-schedulable replicas`、ボリュームの `Scheduled` 条件が `insufficient storage` | PVC が大きすぎて、ディスクの予約（30%）と最低空き容量（25%）を守って置けるノードが足りない。PVC を小さくする（例は [01-storage.md](01-storage.md) の「PVC が置けないとき」）。手順 5 の後、`kubectl -n longhorn-system get nodes.longhorn.io` と UI の Node 画面で各ノードの置ける容量を確認しておく |
| ボリュームが `degraded` のまま | レプリカ 2 つを置けるノード / ディスクの空きが足りない。`kubectl -n longhorn-system get nodes.longhorn.io` と UI の Node 画面を確認 |
| `helmfile destroy` / `helm uninstall longhorn` が失敗する | 安全装置（`deletingConfirmationFlag: false`）による正常な動き。Longhorn を本当に消すときは、データを消してよいか確認してから、Longhorn 公式のアンインストール手順に従う |
| longhorn の Pod が `OOMKilled` | LimitRange の limit 512Mi を超えた。`kubectl -n longhorn-system top pod` で確認し、`platform/values/longhorn.yaml` の LimitRange か manager の limit を上げる |
