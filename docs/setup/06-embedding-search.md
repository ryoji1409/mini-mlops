# フェーズ 6: 画像の埋め込みと LanceDB での検索

フェーズ 5 の GOP 索引を使って、各 GOP の先頭フレーム（キーフレーム）を画像の埋め込みにします。
そのうえで、**テキストで検索 → segment_id の一覧 → フェーズ 5 の選別（`--segment-ids-uri`）→ 学習用データセット** を 1 本の流れにつなぎます。

```
derived/index/gops（フェーズ 5 の索引）
  │ ① embed: キーフレームのチャンクだけを Range GET → デコード（PyAV）→ SigLIP で埋め込み
  ▼
vectors/siglip-base-p16-224__7fd15f0__frames    （1 行 = 1 キーフレーム）
vectors/siglip-base-p16-224__7fd15f0__segments  （1 行 = 1 セグメント。キーフレームの平均）
  │ ② search: テキスト（"night"）や画像を埋め込みにして、近い順に並べる
  ▼
vectors/queries/<名前>.parquet（segment_id の一覧）
  │ ③ select-and-curate -p sql=candidates -p segment-ids-uri=…（フェーズ 5 の選別）
  ▼
curated/<データセット>/v<N>/（manifest とシャード）
```

モデルの重みは MLflow のモデルレジストリに置きます。ホストで 1 回だけ Hugging Face から取って登録し、ジョブは MLflow からダウンロードします。
設計の理由は [docs/architecture.md](../architecture.md) の「フェーズ 6」、用語は [docs/glossary.md](../glossary.md)、
コマンドの詳細は [apps/embedder](../../apps/embedder/README.md) を見てください。

**完了条件**:

1. テキストのクエリごとに、正解値と照らし合わせた precision@10 を表で出す（手順 6）
2. 検索の結果を `--segment-ids-uri` で選別に渡し、manifest とシャードができる（手順 7）
3. 上位のキーフレームを PNG で見て確かめる（手順 8）

## 0. 前提

- フェーズ 5 が終わっていて、`s3://derived/index/gops/` と `s3://derived/tags/segments.parquet` がある
- フェーズ 4 の MLflow が動いている
- ホストのシェルで `.env` を読み込み（`set -a; source .env; set +a`）、S3 の port-forward（`localhost:8333`）と
  MLflow の port-forward（`kubectl -n mlflow port-forward svc/mlflow 5050:5000`）が動いている

```bash
aws s3 ls s3://derived/tags/segments.parquet          # 1 行出れば OK
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5050/health   # 200
```

## 1. S3 の pipeline ユーザーに vectors の読み書きを足す

埋め込みのジョブは `s3://vectors/` に表を書き、選別のジョブは `s3://vectors/queries/` の検索結果を読みます。
pipeline ユーザーに vectors の権限を足しました（[platform/templates/seaweedfs-s3-config.json](../../platform/templates/seaweedfs-s3-config.json)）。
[01-storage.md](01-storage.md) 手順 2 をもう一度実行して Secret を更新し、SeaweedFS を再起動します。

```bash
kubectl -n storage rollout restart deploy/seaweedfs-all-in-one
kubectl -n storage rollout status deploy/seaweedfs-all-in-one --timeout=5m
# 再起動で S3 の port-forward が切れるので、張り直す
AWS_ACCESS_KEY_ID=$PIPELINE_AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$PIPELINE_AWS_SECRET_ACCESS_KEY aws s3 ls s3://vectors/
# エラーが出なければ OK（空なら何も出ない。AccessDenied なら Secret が古い）
```

## 2. モデルの重みを MLflow に登録する（ホストで 1 回だけ）

Hugging Face から `google/siglip-base-patch16-224` をリビジョン `7fd15f0…` に固定して取り、
ファイルごとの sha256 を付けて MLflow に登録し、エイリアス `embedding` を付けます。
登録した重みはホストのキャッシュ（`~/.cache/mini-mlops/models/`）にも置くので、後の検索ではダウンロードし直しません。

```bash
cd apps/embedder && uv sync
MLFLOW_TRACKING_URI=http://localhost:5050 uv run embedder register-model
cd ../..
```

期待される出力:

```
… 重みを MLflow に送ります（数百 MB。サーバー経由で s3://mlflow に入る）
… siglip-base-p16-224 を版 1 として登録しました（リビジョン 7fd15f0）
… 重みをキャッシュにも置きました: /Users/…/.cache/mini-mlops/models/siglip-base-p16-224/v1
{"name": "siglip-base-p16-224", "version": "1", "alias": "embedding", "hf_repo": "google/siglip-base-patch16-224", "hf_revision": "7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed"}
```

- 813MB を port-forward 越しに送るので、10〜30 分かかることがあります（フェーズ 5 で測った port-forward の速さは 0.4〜0.6 MB/s）
- MLflow の画面（`http://localhost:5050` → Models）で `siglip-base-p16-224` の版 1 に、エイリアス `embedding` とタグ `hf_revision` が付いていれば OK
- もう一度実行すると「リビジョン 7fd15f0 は版 1 として登録済みです（重みは登録し直しません）」になり、版は増えません

## 3. イメージをビルドして push する

埋め込みのイメージ（新規）と、索引のイメージ 0.1.1（選別の SQL `candidates` / `candidates_rain` を追加）を作ります。
埋め込みのイメージは indexer をライブラリとして入れるので、**ビルドの起点が `apps/`** です。

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
PLATFORM="${CONVERTER_PLATFORM:-linux/amd64,linux/arm64}"

V=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/indexer/pyproject.toml)
docker buildx build --platform "$PLATFORM" --build-arg INDEXER_VERSION="$V" --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "ghcr.io/${GHCR_USER}/mini-mlops-indexer:${V}" --push apps/indexer

V=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/embedder/pyproject.toml)
docker buildx build --platform "$PLATFORM" --build-arg EMBEDDER_VERSION="$V" --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "ghcr.io/${GHCR_USER}/mini-mlops-embedder:${V}" --push -f apps/embedder/Dockerfile apps
```

埋め込みのイメージは約 1.6GB（PyTorch の CPU 版と transformers。重みは入っていない）です。

## 4. 設定を更新し、WorkflowTemplate を登録する

```bash
kubectl apply -k platform/manifests/pipelines          # ConfigMap pipeline-s3 に VECTORS_URI が増える（他は unchanged）
for f in build-video-index select-and-curate embed-video video-index-and-embed; do
  sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" "pipelines/$f.yaml" | kubectl apply -f -
done
argo template list -n mlops
```

`video-index-and-embed` は、`build-video-index` の手順と `embed-video` の手順を templateRef で順に呼ぶテンプレートです（部品の 2 つを先に登録しておく）。

## 5. 索引に続けて埋め込みを作る

```bash
argo submit -n mlops --from workflowtemplate/video-index-and-embed --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2); echo "$WF"
kubectl -n mlops logs "job/$WF-index" | grep "索引:"
kubectl -n mlops logs "job/$WF-embed" | grep -E "ダウンロード|キャッシュ|埋め込み:|読んだバイト数 [0-9]+（"
```

期待される出力（索引はフェーズ 5 で作成済みなので全部スキップ。埋め込みは手元で試したときの値）:

```
… 索引: 作成 0 / スキップ 30 / 動画なしのセッション 86。読んだバイト数 0（…）
… MLflow から重みをダウンロードします: siglip-base-p16-224 版 1（mlflow-artifacts:/…/artifacts/model）
… 埋め込み: 作成 30 / スキップ 0 セッション、キーフレーム 1800 枚 → siglip-base-p16-224__7fd15f0__frames（セグメント 180 個 → siglip-base-p16-224__7fd15f0__segments）
… 読んだバイト数 160666834（1860 回）= 埋め込んだセッションの MCAP の合計 164416046 の 97.7%
```

- 1 フレーム約 0.23 秒（CPU 2 個）なので、キーフレーム 1,800 枚で 7〜10 分かかります（requests は 500m なので、ノードが混んでいると CPU 1 個分ほどになり、倍くらいかかります）
- 読んだバイト数が MCAP 全体の 98% 近いのは、全部の GOP の先頭フレームを埋め込んでいるからです。
  キーフレーム 1 枚でも、取り出せる最小の単位はチャンク（= 1 GOP、画像の JPEG なども入っている）です。
  `-p gop-stride=5`（5 秒に 1 枚）にすると、読む量も 20% 前後に減ります（手元で 19.7%）

**再実行してもスキップされる** ことを確かめます（埋め込みだけを動かすテンプレート `embed-video`）。

```bash
argo submit -n mlops --from workflowtemplate/embed-video --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
kubectl -n mlops logs "job/$WF-embed" | grep "埋め込み:"
# 埋め込み: 作成 0 / スキップ 30 セッション、キーフレーム 0 枚 → …
aws s3 ls s3://vectors/
#   PRE siglip-base-p16-224__7fd15f0__frames.lance/
#   PRE siglip-base-p16-224__7fd15f0__segments.lance/
```

## 6. precision@10 を測る（完了条件 1）

ホストから、表と正解値を照らし合わせます。重みは手順 2 でキャッシュに置いたものを使います。

```bash
cd apps/embedder
MLFLOW_TRACKING_URI=http://localhost:5050 uv run embedder eval --summary-file ../../output/eval-p10.json
cd ../..
```

期待される出力（手元で試したときの値）:

| クエリ | 正解の条件 | P@10（セグメント平均） | P@10（キーフレーム最大） | 正解の数 / 全体 | でたらめなら |
|---|---|---|---|---|---|
| night | `time_of_day = 'night'` | 1.00 | 1.00 | 60 / 180 | 0.33 |
| dusk | `time_of_day = 'dusk'` | 1.00 | 1.00 | 60 / 180 | 0.33 |
| rain | `weather = 'rain'` | 1.00 | 1.00 | 90 / 180 | 0.50 |
| a clear sunny day | `time_of_day = 'day' AND weather = 'clear'` | 1.00 | 1.00 | 30 / 180 | 0.17 |
| small cylinders nearby | `small_objects_near_any` | 0.50 | 0.50 | 79 / 180 | 0.44 |
| a large cube ahead | `large_objects_ahead_any` | 1.00 | 0.90 | 42 / 180 | 0.23 |

読み方:

- 「でたらめなら」は、180 個から 10 個をでたらめに選んだときの期待値です。これより十分高ければ、検索が効いています
- 時間帯・天候は、画面全体の色・明るさ・雨の線で分かるので、ほぼ当たります
- 近くの小物体は、でたらめとほとんど変わりません。小物体（細い円柱）は 320×180 の画像の中で小さく、
  SigLIP に入れるときに 224×224 に縮めるとさらに小さくなります。またセグメントの平均ベクトルでは、写っている時間が短いと薄まります。
  （`gop-stride` を変えると、平均するキーフレームが変わるので 0.1〜0.2 ほど動きます）
- 進路上の大物体（立方体）は、セッションの間ずっと画面の中央に大きく写るので、よく当たります
- 自分のクエリと正解の条件の組は `--query "…" --truth "…"` で足せます（正解の条件は segments 表の列への SQL）

## 7. 検索 → segment_id の一覧 → 選別 → データセット（完了条件 2）

「night」で検索した上位 20 件を、そのまま学習用のデータセットにします。ホストで検索して結果を S3 に書き、ワークフローで選別します。

```bash
Q=night; OUT="s3://vectors/queries/${Q}-top20.parquet"
(cd apps/embedder && MLFLOW_TRACKING_URI=http://localhost:5050 uv run embedder search --text "$Q" --top-k 20 --min-score 0.01 \
   --out "$OUT" --preview-dir "../../output/search/$Q")
argo submit -n mlops --from workflowtemplate/select-and-curate \
  -p dataset="search-$Q" -p version=1 -p sql=candidates -p segment-ids-uri="$OUT" --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
kubectl -n mlops logs "job/$WF-select" | grep "選別:"
aws s3 ls "s3://curated/search-$Q/v1/" --recursive | awk '{print $4}'
```

期待される出力:

```
| 順位 | segment_id | スコア | 時間帯 | 天候 | 小物体（最大） | 進路上の大物体 |
| 1 | dev-033-…_002 | 0.134 | night | rain | 1 | なし |
…
… 選別: セグメント 20 個 / GOP 200 個 / サンプル 200 件（--frames first）→ s3://curated/search-night/v1（シャード 4 個）
search-night/v1/manifest.json
search-night/v1/segments.parquet
search-night/v1/shards/shard-00000.parquet 〜 shard-00003.parquet
```

- `--out` と同じ名前の `.json`（`s3://vectors/queries/night-top20.json`）に、検索の条件・モデルの版・segment_id の一覧が残ります
- `sql=candidates` は「渡された一覧をそのまま選ぶ」SQL です（[apps/indexer/src/indexer/sql/candidates.sql](../../apps/indexer/src/indexer/sql/candidates.sql)）。
  `sql=candidates_rain` にすると、検索の結果のうち正解値のタグも「雨」のものだけを選びます（検索とタグの組み合わせ）
- manifest の `selection.segment_ids_uri` に、どの検索結果から作ったかが記録されます

```bash
aws s3 cp "s3://curated/search-$Q/v1/manifest.json" - | grep -A3 '"selection"'
```

## 8. 上位のキーフレームを目で確かめる（完了条件 3）

手順 7 の検索で、上位 5 件のキーフレームが `output/search/night/rank-01.png` 〜 `rank-05.png` にできています。

```bash
ls output/search/night/
open output/search/night/rank-01.png      # 画像を開くコマンドは環境に合わせる
```

確かめること:

- 画像は、セグメントの中でクエリに一番近いキーフレーム（MCAP 参照位置から Range GET してデコードしたもの）
- 下の 1 行目が順位・スコア・segment_id・フレーム番号、2 行目が正解値（時間帯/天候・小物体の最大数・進路上の大物体の有無）
- 夜のシーン（暗い背景）で、正解値も `night` になっている

## 9. （任意）画像で似たシーンを探す・条件で絞る

```bash
cd apps/embedder
export MLFLOW_TRACKING_URI=http://localhost:5050
# フェーズ 5 の手順 7 で保存したフレームに似たシーン
uv run embedder search --image ../../output/gop-check/gop-frame-00.png --top-k 5
# テキストと正解値の条件の組み合わせ（雨のセグメントだけから "a large cube ahead" を探す）
uv run embedder search --text "a large cube ahead" --where "weather = 'rain'" --top-k 5
# キーフレーム単位で探す（セグメントの一部にだけ写るものに向く）
uv run embedder search --text "small cylinders nearby" --level frames --top-k 5
cd ../..
```

画像で探すと、同じセッションのセグメントが上位に並びます（同じ時間帯・天候・同じ並びの物体なので）。

クラスタで試したときの結果と読み方:

- 画像: 上位 5 件が、どれも元の画像と同じセッション（昼・雨）のセグメントで、スコア（コサイン類似度）は 0.98 前後
- 雨で絞った "a large cube ahead": 上位 10 件に正解（`large_objects_ahead_any`）が 1 件も入らない（雨の 90 個のうち正解は 12 個）。
  外れた 1 位のセグメントにも、画面の中央に青い立方体が写っている。正解値の「進路上の大物体」は
  「横位置 0（進路の真上）で、前方 40m 以内」の箱だけを数えるので、それより遠い箱・少しずれた箱は、写っていても正解にならない。
  逆に正解のセグメントの箱は、遠くて白く、雨の灰色の空とほとんど見分けが付かない。
  晴れた昼では、近い箱が大きく写るので当たる（条件なしの上位 10 件は全部、晴れた昼の正解）。
  つまり、画像から見て取れることと正解値の定義（距離・横位置）がずれていて、画像の埋め込みでは距離のしきい値までは分からない
- キーフレーム単位の "small cylinders nearby": 上位 5 件のうち 4 件が正解（セグメント平均の P@10 = 0.50 より良い）。
  セグメントの一部にだけ写るものは、キーフレーム単位のほうが向いている

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `register-model` が `Connection refused`（localhost:5050） | MLflow の port-forward が動いていない。`kubectl -n mlflow port-forward svc/mlflow 5050:5000` |
| `register-model` の送信の途中で `Connection reset` / port-forward が切れる | 大きなファイルを port-forward 越しに送っている途中で切れた。port-forward を張り直して再実行する（登録前に止まったなら、版は作られていない） |
| 埋め込みのログに `MLflow に siglip-base-p16-224@embedding がありません` | 手順 2 の登録がまだ。または別の MLflow を見ている（ジョブは `http://mlflow.mlflow.svc:5000`） |
| 埋め込みのログに `Invalid Host header - possible DNS rebinding attack detected` | MLflow の `--allowed-hosts` に `mlflow.mlflow.svc:5000` が入っていない（フェーズ 4 と同じ設定が必要） |
| 埋め込みのジョブが `Suspended` のまま進まず、Argo の待つ側の Pod が `ResourceExhausted desc = EOF` で `Error` になる。`kubectl -n mlops describe workload` に `PodsReadyTimeout` / `DeactivatedDueToRequeuingLimitExceeded` | Kueue は受け入れたが、Pod を置けるノードが無く Pending のまま 10 分たって追い出され、3 回で無効になった。ワーカー（2 vCPU）の requests の空きは 1 CPU 未満なので、Pod の CPU の requests を 1 CPU 未満にする（このテンプレートは 500m）。止まったジョブを消してから流し直す |
| ログに `[transformers] Model config: bos_token_id must be None or an integer within the vocabulary` / `eos_token_id …` | 無害。SigLIP の設定ファイルに書かれている文頭・文末のトークン ID が語彙の範囲外というだけで、埋め込みには使わない |
| 埋め込みのジョブが `OOMKilled` | メモリの上限（2560Mi）を超えた。`kubectl -n mlops describe pod -l job-name=$WF-embed`。まとめて埋め込む枚数を減らす（テンプレートの args に `--batch-size=4`） |
| 埋め込みのジョブの Pod が `Evicted`（ephemeral-storage） | 重みの置き場（emptyDir、上限 2Gi）が足りない、またはノードのディスクが足りない。`kubectl describe node` で空きを確認 |
| `AccessDenied`（s3://vectors） | 手順 1 の Secret 更新と SeaweedFS の再起動がまだ |
| `select` で `No files found that match the pattern "s3://vectors/queries/…"` | 検索の `--out` と `segment-ids-uri` の値が違う、または検索がまだ |
| `select` で `Catalog Error: Table with name candidates does not exist` | `segment-ids-uri` を渡さずに `sql=candidates` を使った |
| `select` で `FileNotFoundError: … sql/candidates.sql` | indexer のイメージが 0.1.0 のまま。手順 3 で 0.1.1 を作り、手順 4 でテンプレートを登録し直す |
| `eval` の値が上の表と大きく違う | 別のモデル・版で埋め込んだ表を見ている（`--model-alias`）、または `gop-stride` を変えた（平均するキーフレームの数が変わる） |
