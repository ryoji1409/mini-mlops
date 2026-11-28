# apps/embedder — キーフレームの画像埋め込みとベクトル検索

## これは何か

動画の各 GOP の先頭フレーム（キーフレーム）を、画像とテキストを同じ空間に埋め込むモデル SigLIP でベクトルにします。
そのベクトルを LanceDB の表（`s3://vectors/`）に入れ、テキストや画像で「似ているシーン」を検索するコマンド `embedder` です（フェーズ 6）。
検索の結果は segment_id の一覧（Parquet）になり、そのまま `indexer select --segment-ids-uri` に渡して学習用のデータセットにできます。

```
GOP 索引（フェーズ 5）──▶ embed: キーフレームだけ Range GET → デコード → SigLIP → s3://vectors/<モデル>__<リビジョン>__frames / __segments
                                                                                       │
テキスト（"night"）/ 画像 ──▶ search ──▶ segment_id の一覧（Parquet）──▶ indexer select --sql candidates --segment-ids-uri … ──▶ curated
```

| コマンド | 中身 |
|---|---|
| `embedder register-model` | 重みを Hugging Face からリビジョン（コミットのハッシュ）を固定して取り、ファイルごとの sha256 を付けて MLflow のモデルレジストリに登録する。エイリアス `embedding` を付ける。ホストで 1 回だけ実行する |
| `embedder embed` | 索引からキーフレームの一覧を作り、Range GET → デコード → 埋め込みを作って表に入れる。同じモデル・リビジョンで埋め込み済みのセッションはスキップする |
| `embedder search` | テキスト（`--text`）か画像（`--image`）で検索する。`--top-k`・`--min-score`・`--where`（正解値などの列の条件）・`--level segments|frames`。`--out` で segment_id の一覧を書き出し、`--preview-dir` で上位のキーフレームを PNG にする |
| `embedder eval` | 正解値と照らし合わせて precision@k を測る（既定のクエリの組は `evaluate.py` の `DEFAULT_QUERIES`） |

| ファイル | 役割 |
|---|---|
| `registry.py` | MLflow への登録（`register_model`）と、エイリアスから重みを取り出してキャッシュする処理（`resolve_model`）。sha256 で壊れた重みを使わない |
| `model.py` | SigLIP の読み込みと埋め込み（画像・テキスト）。テキストと画像が合っている確率（`match_probability`） |
| `keyframes.py` | 索引と正解値から、キーフレームの一覧を作る（DuckDB）。チャンクを Range GET して先頭フレームだけデコードする（`KeyframeReader`、先読みあり） |
| `tables.py` | LanceDB の接続（S3 の設定は `AWS_*` 環境変数から）、表の名前と列、セグメントの平均ベクトル |
| `embed.py` / `search.py` / `evaluate.py` / `cli.py` | 各コマンド |

動画のデコーダ（PyAV）・Range GET・チャンクの解析は、フェーズ 5 の [apps/indexer](../indexer/README.md) をライブラリとして使います（`indexer[video]`）。

### モデル

| 項目 | 値 |
|---|---|
| モデル | `google/siglip-base-patch16-224`（Google、米国。Apache-2.0。ほかのモデルからの派生ではない） |
| リビジョン | `7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed`（`cli.py` の `DEFAULT_HF_REVISION`） |
| 大きさ | 重み 813MB（fp32、約 2 億パラメータ）。埋め込みは 768 次元 |
| 速さ（実測） | コンテナで CPU 2 個: 1 フレーム約 0.23 秒、メモリ最大約 0.9GB |

重みはイメージに入れていません。MLflow のモデルレジストリに置き、ジョブはエイリアスで版を引いて、
`EMBEDDER_MODEL_CACHE`（ジョブでは `/models` の emptyDir、ホストでは `~/.cache/mini-mlops/models`）にダウンロードしてから使います。
同じ版がキャッシュにあり、sha256 が合えばダウンロードしません。インターネット（Hugging Face）に出るのは `register-model` のときだけです。

### 表（`s3://vectors/`）

| 表 | 1 行 | 主な列 |
|---|---|---|
| `<モデル名>__<リビジョン 7 文字>__frames` | 1 キーフレーム | segment_id・log_time_ns・mcap_uri・chunk_offset・chunk_length（MCAP 参照位置）・vector・model・model_version・model_revision・正解値（time_of_day・weather・small_objects_near・large_objects_ahead） |
| `<モデル名>__<リビジョン 7 文字>__segments` | 1 セグメント（10 秒） | キーフレームの平均ベクトル（長さ 1 にそろえ直したもの）・keyframes（平均した枚数）・正解値（フェーズ 5 のタグの値） |

- 表の名前にモデル名とリビジョンを入れているので、モデルを替えると別の表になります（古い表はそのまま残る）。
- 冪等性: セッションごとに「表に入っている GOP の参照位置の集合」と索引を比べ、同じならスキップします。
  間引きの間隔（`--gop-stride`）を変えたり索引を作り直したりして参照位置が変わったセッションは、古い行を消して入れ直します。
- 行数が数千なので、ベクトルの近似索引（IVF-PQ など）は作らず、全件と比べています。

### スコアとしきい値

- テキストで検索したときのスコアは、SigLIP の「テキストと画像が合っている確率」（0〜1）です。
  合成画像では確率の値そのものは小さく（"night" の 1 位でも 0.1 前後）、順位のほうが当てになります。しきい値は 0.01 くらいから試してください。
- 画像で検索したときのスコアは、コサイン類似度（-1〜1）です。

### 検索の単位（`--level`）

- `segments`（既定）: セグメントの平均ベクトルと比べます。夜・雨のような、セグメント全体の雰囲気に向いています。
- `frames`: キーフレームごとに比べ、セグメントの中で一番近いキーフレームの値を使います。セグメントの一部にだけ写るもの（近くの小物体など）に向いています。
- `--where` の条件は、検索する表の列に掛かります（`segments` なら `small_objects_near_max` など、`frames` なら `small_objects_near` など）。

## 実行手順

クラスタでは [docs/setup/06-embedding-search.md](../../docs/setup/06-embedding-search.md) の手順で動かします。ホストでローカルのディレクトリに対して試す:

```bash
cd apps/embedder
uv sync
# フェーズ 5 の索引とタグがあるディレクトリ
export RAW_URI=/tmp/raw DERIVED_URI=/tmp/derived VECTORS_URI=/tmp/vectors
# 重みを手元に取る（MLflow を使わずに試すとき。--model-dir で直接使う）
uv run python -c "from pathlib import Path; from embedder.registry import download_from_hf; from embedder.cli import DEFAULT_HF_REPO, DEFAULT_HF_REVISION; download_from_hf(DEFAULT_HF_REPO, DEFAULT_HF_REVISION, Path('/tmp/models/siglip-base-p16-224'))"
uv run embedder embed --model-dir /tmp/models/siglip-base-p16-224
uv run embedder search --model-dir /tmp/models/siglip-base-p16-224 --text night --top-k 10 --out /tmp/vectors/queries/night.parquet --preview-dir /tmp/preview
uv run embedder eval --model-dir /tmp/models/siglip-base-p16-224
```

### 検索から学習用データセットまで（クラスタ）

テキストで検索 → segment_id の一覧（S3）→ フェーズ 5 の選別（`select-and-curate`）→ manifest とシャード、を続けて実行します。
リポジトリのルートで、`.env` を読み込み、S3（8333）と MLflow（5050）の port-forward が動いている状態で:

```bash
Q=night; OUT="s3://vectors/queries/${Q}-top20.parquet"
(cd apps/embedder && MLFLOW_TRACKING_URI=http://localhost:5050 uv run embedder search --text "$Q" --top-k 20 --min-score 0.01 \
   --out "$OUT" --preview-dir "../../output/search/$Q")
argo submit -n mlops --from workflowtemplate/select-and-curate \
  -p dataset="search-$Q" -p version=1 -p sql=candidates -p segment-ids-uri="$OUT" --watch
WF=$(kubectl -n mlops get wf --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
kubectl -n mlops logs "job/$WF-select" | grep "選別:"
aws s3 ls "s3://curated/search-$Q/v1/" --recursive | awk '{print $4}'
# 期待: 選別: セグメント 20 個 / GOP 200 個 / サンプル 200 件 …（シャード 4 個）
#       search-night/v1/manifest.json、segments.parquet、shards/shard-00000.parquet 〜 00003
```

詳しい手順と期待される出力は [docs/setup/06-embedding-search.md](../../docs/setup/06-embedding-search.md) の手順 7。

## 動作確認コマンド

```bash
uv run pytest -q          # 期待: 4 passed
```

テストは本物の重み（813MB）を使わず、同じ形の小さな乱数のモデルで仕組みだけを確かめます（トークナイザのファイル数 MB だけ Hugging Face から取る）。
MLflow の登録は、サーバーの代わりにローカルのファイルの置き場（`MLFLOW_ALLOW_FILE_STORE=true`）で確かめます。

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `MLflow に siglip-base-p16-224@embedding がありません` | 重みがまだ登録されていない（または `MLFLOW_TRACKING_URI` が別の MLflow を指している）。`embedder register-model`（手順書の手順 2） |
| `モデルのファイルが壊れているか足りません` | ダウンロードが途中で止まった。キャッシュの `v<版>` を消すか、そのまま再実行すると取り直す |
| `… に …__segments がありません（先に embedder embed を実行する）` | 埋め込みがまだか、別のモデル・版の表を見ている。`--model-name` / `--model-alias` と表の名前（`aws s3 ls s3://vectors/`）を確かめる |
| `The filesystem tracking backend … is in maintenance mode` | `MLFLOW_TRACKING_URI` にローカルのディレクトリを指定した。テスト以外では MLflow サーバー（`http://localhost:5050` など）を指定する |
| `Failed to open local file … .mcap`（コンテナで手元のデータを試したとき） | 索引の `mcap_uri` は索引を作ったときの絶対パス。コンテナにも同じパスでマウントする |
| `--text と --image のどちらか 1 つを指定してください` | 検索はテキストか画像のどちらか一方 |
