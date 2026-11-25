# apps/indexer — 動画の GOP 索引・タグ付け・選別

## これは何か

動画入りの MCAP（[apps/mcap-generator](../mcap-generator/README.md) の `--video`）について、次の 3 つを行うコマンド `indexer` です（フェーズ 5）。
クラスタではワークフロー `build-video-index` / `select-and-curate` から、Kueue の data-processing キュー経由の Job として動きます。

| コマンド | 入力 | 出力 | 中身 |
|---|---|---|---|
| `indexer build-index` | raw の MCAP（footer と summary だけ） | `derived/index/gops/device=…/date=…/<session>.parquet`、`index/routes/…`、`index/_manifests/<session>.json` | GOP ごとの「MCAP 参照位置」（チャンクの開始位置と長さ）。予定ルートはルートだけの小さなチャンクを読む |
| `indexer tag` | raw の `ground_truth.parquet`、索引 | `derived/tags/segments.parquet` | フレームごとの正解値をセグメント（10 秒）ごとに集計してタグを付ける（`sql/tag_segments.sql`） |
| `indexer select` | タグ・索引・正解値、選別の SQL | `curated/<名前>/v<N>/manifest.json`、`segments.parquet`、`shards/shard-*.parquet` | SQL でセグメントを選び、1 サンプル = segment_id + GOP の参照位置 + GOP 内のフレーム番号 + 正解値 の一覧を作る |
| `indexer count-truth` | raw の metadata.json だけ | 標準出力 | 条件に合うセグメント数を DuckDB を通さずに数える（選別結果の照合用） |

| ファイル | 役割 |
|---|---|
| `rangefile.py` | S3（またはローカル）のファイルを範囲指定で読む（`RangeReader`）。読んだバイト数と回数を数え（`ReadStats`）、人工的な遅延も入れられる。学習器のローダーも使う |
| `mcap_chunks.py` | MCAP の footer と summary だけを読む（Range GET 2 回）、チャンクの範囲のバイト列からメッセージを取り出す、埋め込まれたスキーマから protobuf のクラスを作る |
| `build_index.py` | 索引の作成。参照位置で実際に GOP を読み、先頭が VPS/SPS/PPS 付きの IDR か確かめる（`--verify-gops`） |
| `tag.py` / `sql/tag_segments.sql` | タグ付け。集計の決まり（最大値・平均値・1 回でも写ったか）は SQL のコメント |
| `select.py` / `sql/*.sql` | 選別とサンプル一覧。フレームの選び方は `--frames first|random|all` |
| `duck.py` | DuckDB の準備（S3 の接続設定、表 `frames`・`gops`・`segments` の定義） |
| `truth.py` | `count-truth` |

選別の SQL は `segment_id` の列を返せばよく、表 `segments`（タグと集計値）・`gops`・`frames` を使えます。
`--segment-ids-uri`（Parquet、`segment_id` 列）を渡すと `candidates` という表も使えます（フェーズ 6 の検索結果を渡す入口）。

依存は mcap・pyarrow・DuckDB だけです。学習器（[apps/trainer](../trainer/README.md)）がライブラリとしても使うので、
pyarrow・protobuf などの版は学習器のイメージ（Ray の公式イメージ）と揃えています。

## 実行手順

クラスタでは [docs/setup/05-video-index.md](../../docs/setup/05-video-index.md) の手順で動かします。ホストでローカルのディレクトリに対して試す:

```bash
cd apps/indexer
uv sync
export RAW_URI=../../output DERIVED_URI=/tmp/derived CURATED_URI=/tmp/curated
uv run indexer build-index
uv run indexer tag
uv run indexer select --dataset night-rain --version 1 --sql night_rain
uv run indexer count-truth --time-of-day night --weather rain
```

S3 に対して動かすときは、`RAW_URI` などを `s3://…` にし、接続先とキーを aws CLI と同じ `AWS_*` 環境変数で渡します。

## 動作確認コマンド

```bash
uv run pytest -q          # 期待: 5 passed（生成器を uv で別に動かして、小さな raw を作ってから試す）
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `MCAP の末尾ではありません（途中で切れたファイル？）` | アップロードが途中で止まった MCAP。`scripts/validate-mcap.sh` で確かめてアップロードし直す |
| `GOP … の参照位置の確認に失敗` | GOP の先頭でチャンクを区切っていない MCAP（`--video` なしで作った、別の生成元など） |
| `チャンクではありません（オペコード 0x…）` | 参照位置がずれている（索引と MCAP の組が違う）。`build-index --force` で作り直す |
| `pyarrow.lib.ArrowTypeError: Unable to merge: Field session_id …`（自分で Parquet を読んだとき） | `pq.read_table` はパスの `session_id=…` も列にしようとして、ファイルの中の列とぶつかる。1 ファイルなら `pq.ParquetFile(path).read()`、DuckDB なら `hive_partitioning = false` |
| `select` で `No files found that match the pattern …segments.parquet` | `tag` を先に実行する |
| `select` で `別の中身で作成済みです` | 同じバージョンに違う条件で書こうとした。`--version` を上げる |
