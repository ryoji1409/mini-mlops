# フェーズ 2: Argo Workflows + KubeRay で MCAP を変換する

raw の MCAP を Ray Data（`read_mcap`）で Parquet / フレームのシャードに変換して derived に書くパイプラインを、
Argo Workflows の WorkflowTemplate `mcap-convert` として動かします。
構成の説明は [platform/README.md](../../platform/README.md)・[pipelines/README.md](../../pipelines/README.md)・[apps/converter/README.md](../../apps/converter/README.md)。

**完了条件**: 全セッションの変換と検証が通り、再実行しても全セッションがスキップされる（結果が変わらない）。
壊れた MCAP は隔離され、他のセッションは変換される。

## 前提

- フェーズ 1（[01-storage.md](01-storage.md)）が終わっていて、port-forward（`localhost:8333`）が動いている
- argo CLI（v4.1.6）と Docker（buildx）がある
- GitHub の classic PAT を 2 つ用意してある: push 用（`write:packages`）と pull 用（`read:packages`）
- コマンドはすべてリポジトリのルートで、`.env` を読み込んだシェルで実行する（`set -a; source .env; set +a`）

## 1. `.env` に GHCR の設定を書く

`.env` の次の行を埋めます（`.env.example` の説明も参照）。

| 変数 | 値 |
|---|---|
| `GHCR_USER` | GitHub のユーザー名（**小文字**。GHCR のイメージ名は小文字のみ） |
| `GHCR_TOKEN` | push 用の PAT（`write:packages`）。ホストでだけ使う |
| `GHCR_PULL_TOKEN` | pull 用の PAT（`read:packages`）。クラスタの Secret に入る |
| `CONVERTER_PLATFORM` | ノードの CPU（例: `linux/arm64`）。空ならイメージを amd64 / arm64 の両方で作る |

```bash
set -a; source .env; set +a
```

## 2. pipeline ユーザーを SeaweedFS に反映する

フェーズ 1 の手順 2 で pipeline ユーザーも登録済みです。キーを変えた / 後から足した場合だけ、
フェーズ 1 の手順 2 をもう一度実行してから SeaweedFS を再起動します（Secret の変更は再起動しないと反映されない）。

```bash
kubectl -n storage rollout restart deploy/seaweedfs-all-in-one
kubectl -n storage rollout status deploy/seaweedfs-all-in-one --timeout=5m
```

`argo-artifacts` バケットはフェーズ 1 の手順 5 で作ってあります（無ければ同じ手順で作る）。

## 3. パイプライン用の Namespace と権限を作る

`platform/manifests/pipelines/` は、Namespace `mlops`、ServiceAccount `mcap-pipeline` と最小限の Role
（`workflowtaskresults` の create/patch、`rayjobs` の create/get/list/watch/patch）、S3 の接続先の ConfigMap、既定リソースの LimitRange です。

```bash
kubectl apply -k platform/manifests/pipelines
```

## 4. Secret を作る

ワークフローと Ray の Pod が使う S3 のキー（pipeline ユーザー）と、GHCR から変換器のイメージを取るための認証です。

```bash
kubectl -n mlops create secret generic pipeline-s3-credentials \
  --from-literal=AWS_ACCESS_KEY_ID="$PIPELINE_AWS_ACCESS_KEY_ID" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$PIPELINE_AWS_SECRET_ACCESS_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n mlops create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username="$GHCR_USER" --docker-password="$GHCR_PULL_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n mlops get secret pipeline-s3-credentials ghcr-pull
```

## 5. Argo Workflows と KubeRay operator を入れる

```bash
helmfile -f platform/helmfile.yaml -l name=argo-workflows -l name=kuberay-operator diff
helmfile -f platform/helmfile.yaml -l name=argo-workflows -l name=kuberay-operator apply
kubectl -n argo rollout status deploy/argo-workflows-workflow-controller --timeout=5m
kubectl -n argo rollout status deploy/argo-workflows-server --timeout=5m
kubectl -n ray-system rollout status deploy/kuberay-operator --timeout=5m
kubectl get crd | grep -E "argoproj.io|ray.io"
```

## 6. 変換器のイメージをビルドして GHCR に push する

タグは `apps/converter/pyproject.toml` の `version` です。このタグが変換器のバージョンとしてマニフェストに記録され、
「同じ版なら再変換しない」判定に使われます。**コードを変えたら version を上げ、同じタグを上書きしない**でください。

```bash
VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/converter/pyproject.toml)
IMAGE=ghcr.io/${GHCR_USER}/mini-mlops-converter:${VERSION}
echo "$IMAGE"

# ログイン（トークンは標準入力で渡し、コマンドラインに出さない）
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

# 同じタグが既にないか確認する（「既にあります」と出たら version を上げる）
docker manifest inspect "$IMAGE" >/dev/null 2>&1 && echo "既にあります: $IMAGE" || echo "まだありません: $IMAGE"

# ビルドして push（コミットしていない変更があると、イメージのラベルの revision と中身がずれる）
git status --short apps/converter
docker buildx build \
  --platform "${CONVERTER_PLATFORM:-linux/amd64,linux/arm64}" \
  --build-arg CONVERTER_VERSION="$VERSION" \
  --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "$IMAGE" --push apps/converter
docker buildx imagetools inspect "$IMAGE" | head -4
```

初めて push したパッケージは非公開で作られます。GitHub の Packages 画面で Private になっていることを確認します。

## 7. WorkflowTemplate を登録する

`pipelines/mcap-convert.yaml` のイメージ名 `ghcr.io/OWNER/...` の `OWNER` を自分のユーザー名に置き換えて登録します
（タグは pyproject.toml の version と揃えてあり、変換器のテストで一致を確かめています）。

**フェーズ 3 以降のテンプレートは、RayJob を Kueue のキュー経由で動かします**（`suspend: true` で作り、Kueue が受け入れたら動く）。
まっさらなクラスタで順に進めている場合は、この手順の前に [03-kueue.md](03-kueue.md) の手順 2〜3 で Kueue とキューを入れておきます
（入れないと RayJob が `Suspended` のまま動きません）。

```bash
sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" pipelines/mcap-convert.yaml | kubectl apply -f -
kubectl -n mlops get workflowtemplate mcap-convert
```

## 8. 実行する（完了条件）

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert --watch
```

`--watch` で各ステップの進み具合が見えます。終わったら結果を確認します。

```bash
argo get -n mlops @latest
argo logs -n mlops @latest | grep -E "変換対象|OK |QUARANTINED|MISMATCH|MISSING|検証"
```

期待される出力（1 回目、抜粋）:

```
STEP                      TEMPLATE   PODNAME   DURATION  MESSAGE
 ✔ mcap-convert-xxxxx     main
 ├───✔ discover           discover   ...
 ├───✔ convert            convert    ...                             ← RayJob が作られ、終わると Ray クラスタは消える
 └───✔ validate           validate   ...

セッション 6 件: 変換対象 6 / 変換済み 0 / 隔離済み 0 / metadata 不正 0
  OK          dev-001-20261104T013238Z         imu=3000, gnss=300, frames_front=150
  ...
検証結果: {"sessions": 6, "ok": 6, "quarantined": 0, "mismatch": 0, "missing": 0, "orphans": 0, "invalid_metadata": 0, "unreadable_datasets": 0}
検証 OK
```

もう一度実行して、**全セッションがスキップされる**ことを確かめます（冪等性）。

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert --watch
argo logs -n mlops @latest | grep -E "変換対象|検証結果"
```

期待される出力（2 回目）:

```
 ├───○ convert            convert    when '0 > 0' evaluated false     ← スキップ（Ray クラスタを立てない）
セッション 6 件: 変換対象 0 / 変換済み 6 / 隔離済み 0 / metadata 不正 0
検証結果: {"sessions": 6, "ok": 6, "quarantined": 0, ...}
```

derived の中身:

```bash
aws s3 ls s3://derived/ --recursive | head -20
aws s3 ls s3://derived/_manifests/
aws s3 cp s3://derived/_manifests/dev-001-20261104T013238Z.json - | python3 -m json.tool | head -40
```

## 9. 実行中の様子を見る

Argo の UI（認証モード server。ClusterIP のまま port-forward でだけ開く）:

```bash
kubectl -n argo port-forward svc/argo-workflows-server 2746:2746     # → http://localhost:2746（Namespace は mlops を選ぶ）
```

Ray Dashboard（convert ステップの RayJob が動いている間だけ。ジョブ終了の 120 秒後に Ray クラスタが消える）:

```bash
kubectl -n mlops get rayjob -w                                        # RayJob ができるのを待つ（Ctrl-C）
RAYJOB=$(kubectl -n mlops get rayjob --sort-by=.metadata.creationTimestamp -o name | tail -1)
CLUSTER=$(kubectl -n mlops get "$RAYJOB" -o jsonpath='{.status.rayClusterName}')
kubectl -n mlops wait --for=condition=Ready pod -l "ray.io/cluster=${CLUSTER},ray.io/node-type=head" --timeout=5m
kubectl -n mlops port-forward "svc/${CLUSTER}-head-svc" 8265:8265     # → http://localhost:8265
```

- **Jobs**: `converter convert` のジョブとログ（事前チェック、隔離、変換結果）
- **Ray Data**（Jobs → ジョブを開く）: ReadMCAP → Sort → MapBatches(_convert_group) の進み具合
- **Cluster**: head 1 台 + worker 2 台のノードごとの CPU / メモリ

コマンドで見る場合:

```bash
kubectl -n mlops get workflow,rayjob,raycluster,pod
kubectl -n mlops logs job/<RayJob 名> -f            # ジョブのログ（RayJob 名は argo get の convert の出力 rayjob-name）
kubectl -n mlops top pod                            # head < 3Gi、worker < 1.25Gi か
```

## 10. 壊れた MCAP の隔離を確認する（完了条件）

正常なセッションの MCAP を途中で切った「壊れたセッション」`dev-901-20261101T000000Z` を作り、raw に置きます。
metadata.json の sha256 は切ったあとのファイルに合わせてあるので、sha256 の照合は通り、MCAP の構造チェックで隔離されます。
（`upload-raw.sh` の検証は通らないので、aws CLI で直接置きます。MCAP → metadata.json の順に置く）

```bash
SRC=$(dirname "$(find output -name metadata.json -path '*session_id=*' | sort | head -1)")
BROKEN_DIR=$(mktemp -d)
REL=$(python3 -I scripts/lib/make_broken_session.py "$SRC" "$BROKEN_DIR" | tail -1)
echo "$REL"                                   # device=dev-901/date=2026-11-01/session_id=dev-901-20261101T000000Z
aws s3 cp "$BROKEN_DIR/$REL/dev-901-20261101T000000Z.mcap" "s3://raw/$REL/"
aws s3 cp "$BROKEN_DIR/$REL/metadata.json" "s3://raw/$REL/"
```

実行して、壊れた 1 本だけが隔離され、他の 6 本は OK のままであることを確かめます。

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert --watch
argo logs -n mlops @latest | grep -E "変換対象|QUARANTINED|検証結果"
aws s3 cp s3://derived/_quarantine/dev-901-20261101T000000Z.json - | python3 -m json.tool
```

期待される出力:

```
セッション 7 件: 変換対象 1 / 変換済み 6 / 隔離済み 0 / metadata 不正 0
  QUARANTINED dev-901-20261101T000000Z         preflight: RuntimeError: RecordLengthLimitExceeded: ...（切った位置によってエラーの種類は変わる）
検証結果: {"sessions": 7, "ok": 6, "quarantined": 1, ...}
```

ワークフローは成功のまま（隔離は失敗扱いにしない）。隔離があったら失敗にしたいときは `-p fail-on-quarantine=true` を付けます。
もう一度実行すると、隔離済みのセッションもスキップされます（`-p force=true` で作り直し・再挑戦）。

### 片付け

**raw のデータを消す操作です。** 消してよいことを確認してから実行します。

```bash
aws s3 ls s3://raw/device=dev-901/ --recursive                 # 消すものを確認
aws s3 rm s3://raw/device=dev-901/date=2026-11-01/session_id=dev-901-20261101T000000Z/ --recursive
aws s3 rm s3://derived/_quarantine/dev-901-20261101T000000Z.json
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `docker manifest inspect` で「既にあります」 | 同じタグのイメージがある。コードを変えたなら `apps/converter/pyproject.toml` の version と `pipelines/mcap-convert.yaml` のタグを上げる（上げると全セッションが再変換される） |
| push で `denied: permission_denied: write_package` | `GHCR_TOKEN` に write:packages が無い。PAT を作り直す |
| Pod が `ImagePullBackOff`、`describe` に `401 Unauthorized` | `GHCR_PULL_TOKEN` が無い / 権限不足。手順 4 の `ghcr-pull` を作り直す |
| `ImagePullBackOff` で `no match for platform in manifest` | イメージの CPU アーキテクチャがノードと違う。`CONVERTER_PLATFORM` をノードに合わせて手順 6 からやり直す |
| Pod が `InvalidImageName` / `ghcr.io/OWNER/...` を取りに行く | 手順 7 の置き換えをせずに登録した。手順 7 をやり直す |
| discover が `AccessDenied` / `InvalidAccessKeyId` | pipeline ユーザーのキーが SeaweedFS に登録されていない。手順 2（フェーズ 1 の手順 2 と再起動） |
| ワークフローのステップが `failed to get artifact repository` / ログ保存で `AccessDenied` | Secret `pipeline-s3-credentials` が無い、`argo-artifacts` バケットが無い、または SeaweedFS の再起動忘れ |
| convert が `forbidden: ... cannot create resource "rayjobs"` | Role が無い。手順 3 |
| ワークフローが `Pending` のまま、`waiting for lock` | 同じデバイスの mcap-convert が動いている（デバイスごとに 1 つずつ実行）。`argo list -n mlops` |
| convert が終わらず、RayJob が `Suspended` のまま | フェーズ 3 版のテンプレートは Kueue のキューを通る。Kueue が入っていなければ [03-kueue.md](03-kueue.md) 手順 2〜3 で入れる |

変換器・ワークフロー固有のエラーは [pipelines/README.md](../../pipelines/README.md) と [apps/converter/README.md](../../apps/converter/README.md) の「よくあるエラー」を参照。
