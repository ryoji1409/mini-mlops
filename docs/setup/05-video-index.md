# フェーズ 5: 動画の索引・データの選別・高速な読み出し

MCAP に H.265 の動画（GOP = 1 秒）と予定ルートを入れ、
**MCAP を丸ごとダウンロードせずに、必要な GOP だけを S3 の Range GET で取り出して学習に流す** 仕組みを作ります。

```
raw（MCAP + 正解値）
  │ ① build-index: footer と summary だけを Range GET → GOP ごとの「MCAP 参照位置」（チャンクの開始位置と長さ）
  ▼
derived/index/gops/、routes/
  │ ② tag: フレームごとの正解値をセグメント（10 秒）ごとに集計してタグを付ける（DuckDB）
  ▼
derived/tags/segments.parquet
  │ ③ select: SQL ファイル（夜かつ雨、など）でセグメントを選び、サンプル一覧（manifest）をシャードに分ける
  ▼
curated/<データセット>/v<N>/
  │ ④ 学習のローダー: サンプルの GOP のチャンクだけを Range GET → デコード（PyAV）→ 経路ラスタを描く。先読みあり
  ▼
1 サンプル = カメラ画像 + 経路ラスタ（128×128、0.5 m/px）+ 正解値
```

設計の理由（チャンク = GOP にした理由と、その代償など）は [docs/architecture.md](../architecture.md) の「フェーズ 5」、
用語は [docs/glossary.md](../glossary.md)、各アプリは [apps/indexer](../../apps/indexer/README.md)・
[apps/navi-raster](../../apps/navi-raster/README.md)・[apps/trainer](../../apps/trainer/README.md)・[apps/mcap-generator](../../apps/mcap-generator/README.md)。

**完了条件**:

1. 索引を作るときに読んだバイト数が、MCAP 全体よりずっと少ない（手順 6）。索引の参照位置だけで GOP をデコードできる（手順 7）
2. DuckDB の「夜かつ雨」の選別件数が、正解値（metadata.json から数えた数）と一致する（手順 9）
3. ローダーの 1 エポックで S3 から読んだバイト数が、MCAP の合計よりずっと少ない（ログで示す。手順 10）
4. 先読みあり / なしの samples/sec を比べる（人工的な遅延 0ms / 50ms。手順 10）
5. 「カメラ画像 + 経路ラスタ」の PNG を数枚保存して、目で確かめる（手順 11）

## 前提

- フェーズ 4（[04-training.md](04-training.md)）が終わっている
- `.env` を読み込み、S3 の port-forward が動いている（[01-storage.md](01-storage.md) 手順 4）
- 手元に `uv` と Docker（buildx）がある

```bash
set -a; source .env; set +a
```

## 1. 動画入りのセッションを生成して、検証・アップロードする

既存のセッション（dev-001〜dev-026）はそのまま残し、動画入りのセッションを新しいデバイス番号で追加します。
6 デバイス（dev-031〜dev-036）× 5 セッション × 60 秒 = 30 セッション・180 セグメント。
`--video` を付けたときだけ、動画（`/camera/front/compressed_video`）・予定ルート（`/navigation/route`）・
フレームごとの正解値（`ground_truth.parquet`）が増えます（付けなければ以前と 1 バイトも変わらない）。

```bash
(cd apps/mcap-generator && uv sync && uv run mcap-generate --video --first-device 31 --devices 6 --sessions-per-device 5 \
  --min-duration 60 --max-duration 60 --out ../../output --seed 31)
# [30/30] dev-036-20261108T023048Z day/clear 大0 小2 60s 4.7MB (…, /camera/front/compressed_video=600, …, /navigation/route=1) 1.4s
```

MCAP の中身を確かめます（チャンクの数 = 61 = ルート用の 1 + GOP 60 個）。

```bash
mcap info "$(ls output/device=dev-031/*/*/*.mcap | head -1)" | grep -E "chunks|compressed_video|route|zstd"
```

期待される出力:

```
	zstd: [61/61 chunks] …
	(1) /navigation/route             	   1 msgs                   	 : foxglove.LocationFixes [protobuf]
	(2) /camera/front/compressed_video	 600 msgs (9.98..10.00Hz)   	 : foxglove.CompressedVideo [protobuf]
```

検証とアップロード（付属ファイル `ground_truth.parquet` も、サイズと sha256 を metadata.json と照合し、metadata.json より先に置く）。

```bash
scripts/validate-mcap.sh output                 # 最後に OK=116 NG=0（既存 86 + 追加 30）
scripts/upload-raw.sh output                    # 既存の 86 本は「スキップ」、追加の 30 本（+ ground_truth.parquet）がアップロードされる
aws s3 ls "s3://raw/device=dev-031/" --recursive | head -3
```

期待される出力（1 セッション = 3 ファイル）:

```
… dev-031-…/dev-031-….mcap
… dev-031-…/ground_truth.parquet
… dev-031-…/metadata.json
```

## 2. S3 の training ユーザーに raw / derived の読み取りを足す

学習のローダーは、MCAP（raw）のチャンクと予定ルート（derived）を直接読むので、training ユーザーに読み取りの権限を足しました
（[platform/templates/seaweedfs-s3-config.json](../../platform/templates/seaweedfs-s3-config.json)。書き込みは curated だけのまま）。
[01-storage.md](01-storage.md) 手順 2 をもう一度実行して Secret を更新し、SeaweedFS を再起動します。

```bash
kubectl -n storage rollout restart deploy/seaweedfs-all-in-one
kubectl -n storage rollout status deploy/seaweedfs-all-in-one --timeout=5m
AWS_ACCESS_KEY_ID=$TRAINING_AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$TRAINING_AWS_SECRET_ACCESS_KEY \
  aws s3 ls "s3://raw/device=dev-031/" | head -2          # 一覧が出れば OK（AccessDenied なら Secret が古い）
```

## 3. 既存の変換が、新しいトピックの入った MCAP でも動くことを確かめる

変換器（フェーズ 2）は、知らないトピック（動画・予定ルート）を「無視したトピック」として記録して飛ばします。

```bash
argo submit -n mlops --from workflowtemplate/mcap-convert --watch
argo logs -n mlops @latest | grep -E "変換対象|検証結果"
# 検証結果: {"sessions": 117, "ok": 116, "quarantined": 1, ...}
aws s3 cp "s3://derived/_manifests/$(aws s3 ls s3://derived/_manifests/ | grep dev-031 | head -1 | awk '{print $4}')" - | grep -A3 ignored_topics
# "ignored_topics": { "/navigation/route": 1, "/camera/front/compressed_video": 600 }
```

## 4. イメージをビルドして push する

索引のイメージ（新規）と、学習器 0.2.0（動画ローダー入り）を作ります。
学習器は同じリポジトリのライブラリ（apps/indexer・apps/navi-raster）も入れるので、**ビルドの起点が `apps/`** です。

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
PLATFORM="${CONVERTER_PLATFORM:-linux/amd64,linux/arm64}"

V=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/indexer/pyproject.toml)
docker buildx build --platform "$PLATFORM" --build-arg INDEXER_VERSION="$V" --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "ghcr.io/${GHCR_USER}/mini-mlops-indexer:${V}" --push apps/indexer

V=$(sed -n 's/^version = "\(.*\)"$/\1/p' apps/trainer/pyproject.toml)
docker buildx build --platform "$PLATFORM" --build-arg TRAINER_VERSION="$V" --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)" \
  --tag "ghcr.io/${GHCR_USER}/mini-mlops-trainer:${V}" --push -f apps/trainer/Dockerfile apps
```

## 5. 権限を追加し、WorkflowTemplate を登録する

```bash
kubectl apply -k platform/manifests/pipelines          # ServiceAccount curate-pipeline が増える（他は unchanged）
for f in build-video-index select-and-curate; do
  sed "s#ghcr.io/OWNER/#ghcr.io/${GHCR_USER}/#" "pipelines/$f.yaml" | kubectl apply -f -
done
argo template list -n mlops
```

## 6. GOP の索引を作る（build-index → tag）

```bash
argo submit -n mlops --from workflowtemplate/build-video-index --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2); echo "$WF"
kubectl -n mlops logs "job/$WF-index" | tail -2
kubectl -n mlops logs "job/$WF-tag"
```

各ステップは Job を作って終わるのを待つだけなので、`argo logs` には待機の記録しか出ません。
処理のログは Job（`<ワークフロー名>-index` / `-tag`）から見ます（Job は終了後 1 時間で消えます）。

期待される出力（セッションごとの行は 30 行。ここでは最後の 1 行だけ）:

```
… dev-036-…: GOP 60 個・ルート 48 点。読んだバイト数 108233（summary 12195 / ルート 1397 / 確認 1 GOP）= MCAP 全体 5798644 の 1.9%
… 索引: 作成 30 / スキップ 0 / 動画なしのセッション 86。読んだバイト数 3081549（索引を作った MCAP の合計 164416046 の 1.87%）
… タグ付け: セグメント 180 個 → s3://derived/tags/segments.parquet
…   clear                90
…   day                  60
…   dusk                 60
…   large_object_ahead   42
…   night                60
…   rain                 90
…   small_objects        79
```

- 「確認 1 GOP」は、索引の参照位置で実際に 1 GOP を読み、先頭が VPS/SPS/PPS 付きの IDR か確かめた分（約 95KB）
- 「動画なしのセッション」の数は、それまでのフェーズで上げたセッション数によって変わる
- バイト数・サイズは環境によって少し違ってもよい（目安は MCAP 全体の 2% 前後）

**再実行しても作り直さない** ことを確かめます（同じ MCAP の sha256・同じ索引の版ならスキップ。S3 から読むのは metadata.json だけ）。

```bash
argo submit -n mlops --from workflowtemplate/build-video-index --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2); echo "$WF"
kubectl -n mlops logs "job/$WF-index" | grep "索引:"
# 索引: 作成 0 / スキップ 30 / 動画なしのセッション 86。読んだバイト数 0（…）
aws s3 ls s3://derived/index/ --recursive | awk '{print $4}' | cut -d/ -f1-2 | sort | uniq -c
#   30 index/_manifests
#   30 index/gops
#   30 index/routes
```

## 7. 索引の参照位置だけで GOP をデコードできることを確かめる

索引の 1 行（GOP の参照位置）を取り出し、その範囲だけを Range GET してデコードします。

```bash
cd apps/trainer && uv sync
read U O L < <(uv run python -c "
import pyarrow.parquet as pq, pyarrow.fs as fs, os
s3 = fs.S3FileSystem(endpoint_override=os.environ['AWS_ENDPOINT_URL'], region=os.environ.get('AWS_REGION', 'us-east-1'))
path = [i.path for i in s3.get_file_info(fs.FileSelector('derived/index/gops', recursive=True)) if i.path.endswith('.parquet')][0]
r = pq.ParquetFile(s3.open_input_file(path)).read().to_pylist()[25]  # read_table だとパスの date= と列の date がぶつかる
print(r['mcap_uri'], r['chunk_offset'], r['chunk_length'])")
echo "$U $O $L"
uv run trainer decode-gop --mcap-uri "$U" --offset "$O" --length "$L" --out-dir ../../output/gop-check
cd ../..
```

期待される出力:

```
… 先頭のメッセージの NAL: [32, 33, 34, 39, 20]（32/33/34 = VPS/SPS/PPS、19/20 = IDR）
… デコードしたフレーム: 10 / 10。読んだバイト数 117593（summary 12195 + チャンク 105398）= MCAP 全体 6437260 の 1.8%
… 先頭と最後のフレームを ../../output/gop-check に保存しました
```

参照位置を 1 バイトずらすと、チャンクの先頭ではないので失敗します（`--offset "$((O+1))"` で `チャンクではありません（オペコード 0x…）`）。

## 8. 選別の SQL を確かめる

選別の条件は SQL ファイル（[apps/indexer/src/indexer/sql/](../../apps/indexer/src/indexer/sql/)）に書きます。
SQL は `segment_id` の列を返せばよく、使える表は `segments`（タグと集計値）・`gops`（索引）・`frames`（フレームごとの正解値）です。

```bash
cat apps/indexer/src/indexer/sql/night_rain.sql
```

## 9. 「夜かつ雨」を選別し、正解値と照合する（完了条件 2）

```bash
argo submit -n mlops --from workflowtemplate/select-and-curate -p dataset=night-rain -p version=1 -p sql=night_rain --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2); echo "$WF"
kubectl -n mlops logs "job/$WF-select" | grep "選別:"
```

期待される出力:

```
… 選別: セグメント 30 個 / GOP 300 個 / サンプル 300 件（--frames first）→ s3://curated/night-rain/v1（シャード 6 個）
```

正解値を、**DuckDB も索引も通さずに** metadata.json だけから数え直して比べます（ホストから）。

```bash
(cd apps/indexer && uv sync && uv run indexer count-truth --time-of-day night --weather rain)
# {"matched_segments": 30, "video_segments": 180}       ← 選別の「セグメント 30 個」と一致すれば OK
aws s3 ls s3://curated/night-rain/v1/ --recursive | awk '{print $4}'
# night-rain/v1/manifest.json / night-rain/v1/segments.parquet / night-rain/v1/shards/shard-00000.parquet 〜 00005
```

同じ条件でもう一度実行すると「同じ中身の … が既にあるのでスキップします」、
条件を変えて同じバージョンに書こうとすると（例: `-p frames=all`）「別の中身で作成済みです。--version を上げてください」で失敗します。

セッションの一部だけが選ばれる例（小物体が近くに写るセグメント）も作っておきます（手順 10 の比較に使う）。

```bash
argo submit -n mlops --from workflowtemplate/select-and-curate -p dataset=small-objects -p version=1 -p sql=small_objects --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2); echo "$WF"
kubectl -n mlops logs "job/$WF-select" | grep "選別:"
# … 選別: セグメント 79 個 / GOP 790 個 / サンプル 790 件（--frames first）→ s3://curated/small-objects/v1（シャード 16 個）
```

## 10. ローダーの読み出し量と、先読みの効果を測る（完了条件 3・4）

ホストから、training ユーザーのキーで動かします（port-forward 越しの S3 なので、遅延はクラスタ内より大きめ）。
`--latency-ms` は 1 回の読み出しごとに足す人工的な遅延、`--prefetch` は先読みする GOP の数（0 = 先読みなし）です。

```bash
cd apps/trainer
export AWS_ACCESS_KEY_ID=$TRAINING_AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$TRAINING_AWS_SECRET_ACCESS_KEY
uv run trainer bench-video --manifest-uri s3://curated/night-rain/v1/manifest.json \
  --latency-ms 0 50 --prefetch 0 4 --preview-dir ../../output/preview --summary-file ../../output/bench-night-rain.json
uv run trainer bench-video --manifest-uri s3://curated/small-objects/v1/manifest.json \
  --latency-ms 0 --prefetch 4 --summary-file ../../output/bench-small-objects.json
set -a; source ../../.env; set +a                # キーを元に戻す
cd ../..
```

期待される出力（手元のディレクトリで試したときの値。samples/sec は環境で大きく変わる）:

```
… latency=  0ms prefetch=0: 300 サンプル / 0.3 秒 = 874.4 samples/sec、読んだバイト数 28215955（321 回）
… latency=  0ms prefetch=4: 300 サンプル / 0.2 秒 = 1237.1 samples/sec、読んだバイト数 28215955（321 回）
… latency= 50ms prefetch=0: 300 サンプル / 18.0 秒 = 16.7 samples/sec、読んだバイト数 28215955（321 回）
… latency= 50ms prefetch=4: 300 サンプル / 5.0 秒 = 59.4 samples/sec、読んだバイト数 28215955（321 回）
… 読んだバイト数 28215955 = 選ばれたセッションの MCAP の合計 28770165（5 ファイル）の 98.1% / raw の全 MCAP 166506083（31 ファイル）の 16.95%
（small-objects）
… 読んだバイト数 71000562 = 選ばれたセッションの MCAP の合計 114116416（21 ファイル）の 62.2% / raw の全 MCAP 166506083（31 ファイル）の 42.64%
```

読み方:

- 読んだバイト数は、先読みの有無・遅延に関係なく同じ（読む範囲は manifest で決まる）。回数 321 = GOP 300 + MCAP の summary（5 ファイル × 2 回）+ ルート 5 + シャード 6
- night-rain は「夜かつ雨」がセッション単位の条件なので、選ばれたセッションの GOP を全部読む（選ばれたセッションの MCAP とほぼ同じ量）。
  それでも raw 全体の 17%。small-objects はセッションの一部だけなので、選ばれたセッションの MCAP の 62%
- **raw の全 MCAP** は、動画なしの既存セッション（86 本）も含む。比べる相手を「選ばれたセッション」と「全部」の 2 つにしているのはこのため
- 遅延 50ms では、先読みなしは「GOP の数 × 50ms」がそのまま待ち時間になる。先読み 4 で約 4 倍に近づく
- ホストから port-forward 越しに読むと、帯域（0.5MB/s 前後）で詰まり、1 回の計測に 1〜2 分かかる。50 サンプルごとに途中経過が出る。
  このときは先読みの効果が小さく出る（例: 0ms で 4.4 → 6.4、50ms で 2.9 → 5.2 samples/sec）。遅延ではなく帯域が詰まっているため、先読みで隠せる部分が少ない

## 11. カメラ画像と経路ラスタを目で確かめる（完了条件 5）

手順 10 で `output/preview/sample-00.png` 〜 `sample-05.png` ができています。

```bash
ls output/preview/
open output/preview/sample-03.png      # 画像を開くコマンドは環境に合わせる
```

確かめること:

- 左がカメラ画像（320×180。夜・雨のサンプルなので暗く、雨の線がある）、右が経路ラスタ（128×128 を拡大）
- 経路ラスタの自分の位置（灰色の三角）は下から 1/4、進行方向が上。これから進むルートが白、通り過ぎたルートが灰色
- 下の文字に segment_id・フレーム番号・正解値・向きの決め方（`heading=gnss` か `heading=route`）が出る

## 12. （任意）外から渡した segment_id で選別する

フェーズ 6 の検索結果のように、別の仕組みで選んだ segment_id の一覧（Parquet、`segment_id` 列）を
`segment-ids-uri` で渡すと、SQL から `candidates` という表で使えます（タグの条件と組み合わせられる）。

indexer 0.1.1 からは、渡した一覧をそのまま選ぶ `sql=candidates` と、そのうち雨のものだけを選ぶ `sql=candidates_rain` がイメージに入っています。
検索の結果から選別する一連の手順は [06-embedding-search.md](06-embedding-search.md) の手順 7 にあります。
ほかの条件と組み合わせたいときは、SQL ファイルを書きます。

```bash
cat > /tmp/from_candidates.sql <<'EOF'
SELECT segment_id FROM segments JOIN candidates USING (segment_id) WHERE time_of_day = 'night'
EOF
```

SQL ファイルはイメージに入っているものだけが使えるので、ワークフローから使うときは `apps/indexer/src/indexer/sql/` に置いてイメージを作り直します
（ホストからなら `uv run indexer select … --sql /tmp/from_candidates.sql --segment-ids-uri s3://…` でそのまま試せる）。

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `mcap-generate --video` で `ModuleNotFoundError: No module named 'av'` | 生成器の依存が古い。`cd apps/mcap-generator && uv sync` |
| `validate-mcap.sh` で `付属ファイルの sha256 不一致: ground_truth.parquet` | 生成し直した MCAP と付属ファイルの組が混ざっている。そのセッションのディレクトリを消して生成し直す |
| `argo logs` に `Waiting for conditions` などの待機の記録しか出ない | 索引・タグ付け・選別のステップは Job を作って待つだけで、処理のログは Job 側にある。`kubectl -n mlops logs job/<ワークフロー名>-index`（`-tag` / `-select`）。Job は終了後 1 時間で消える |
| 索引の Parquet を `pq.read_table` で読むと `Unable to merge: Field date has incompatible types` | パスの `date=…` を pyarrow がパーティション列として足し、ファイル内の `date` 列と型がぶつかる。`pq.ParquetFile(...).read()` で 1 ファイルとして読む（DuckDB なら `hive_partitioning=false`） |
| 索引のログで `索引: 作成 0 / … 動画なしのセッション 116` | 動画入りのセッションがアップロードされていない（手順 1）。`aws s3 ls s3://raw/device=dev-031/ --recursive` |
| `GOP … の参照位置の確認に失敗` | MCAP が GOP の先頭でチャンクを区切っていない（`--video` なしで作った、または別の生成元）。`mcap info` でチャンク数がおよそ「秒数 + 1」か確かめる |
| `select` で `No files found that match the pattern "s3://derived/tags/segments.parquet"` | タグ付けがまだ。`build-video-index` を先に実行する |
| `select` で `別の中身で作成済みです` | 同じデータセット・バージョンに、違う条件で書こうとした。`-p version=` を上げる |
| `bench-video` で `AccessDenied`（raw や derived） | training ユーザーの権限が古い（手順 2 の Secret 更新と SeaweedFS の再起動） |
| `bench-video` で `KeyError: 'route_uri'` | indexer 0.1.0 より前の形式の manifest。選別をやり直して新しいバージョンで作る |
| `uv run` の中で Ray を起動した学習器のテストで、ワーカーが `Failed to generate package metadata for indexer @ directory+../indexer` | Ray が `uv run` の環境をワーカーで作り直すとき、作業ディレクトリの複製先では `../indexer` が見つからない。`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` を付けて起動する（テストは設定済み） |
| 学習器イメージのビルドで `COPY failed: … indexer/pyproject.toml: not found` | ビルドの起点が `apps/trainer` になっている。`-f apps/trainer/Dockerfile apps`（手順 4） |
