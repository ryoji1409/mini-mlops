# apps/dataset-builder — 学習用データセットの作成

## これは何か

derived ゾーンの変換済みフレーム（`frames/front`）と、raw ゾーンの `metadata.json`（時間帯・天候）を結び付け、
curated ゾーンに **バージョン付き** の学習用データセット `s3://curated/scene-cls/v<N>/` を作ります。
クラスタでは、ワークフロー `train-scene-classifier` の最初のステップとして、batch/v1 Job（Kueue の data-processing キュー経由）で動きます。

```
s3://curated/scene-cls/v<N>/
  train/part-00000.parquet … part-00007.parquet   学習用（行の順番を混ぜてから 8 個に分割。ワーカーごとに別のファイルを読む）
  val/part-00000.parquet, part-00001.parquet      検証用
  manifest.json                                   最後に書く（= 完成の印）
```

| 列 | 中身 |
|---|---|
| `image` | 160x90 に縮小した JPEG（元は 640x360） |
| `time_of_day` | 0 = day、1 = dusk、2 = night |
| `weather` | 0 = clear、1 = rain |
| `session_id` / `device_id` / `frame_index` | どのセッションの何枚目か |

### 学習用と検証用の分け方（セッション単位）

同じセッションの連続したフレームはほとんど同じ見た目です。フレーム単位でランダムに分けると、学習で見た画像とほぼ同じ画像で答え合わせをすることになり、
精度が不当に高く出ます。そこで **セッションごとに丸ごと** 学習用か検証用に入れます。
検証用に全クラスが入るよう、（時間帯, 天候）の組ごとに `--val-per-combo`（既定 2）セッションを検証用に回します（同じ seed なら同じ分け方）。

### マニフェスト（`manifest.json`）

| 項目 | 中身 |
|---|---|
| `inputs` | 使ったセッションごとの、derived のマニフェストのパスとその sha256、変換器の版、ラベル、学習用 / 検証用 |
| `splits` | 学習用・検証用それぞれのセッション、フレーム数、クラスごとの内訳、ファイル（パス・行数・sha256） |
| `params` | 画像サイズ、JPEG の品質、検証用のセッション数、シャード数、乱数シード |
| `input_hash` | 入力（セッションごとのフレームのファイルの sha256 とラベル）と作り方から計算したハッシュ。同じなら作り直さない。derived のマニフェストのファイル全体の sha256 は使わない（変換し直すと `converted_at` だけが変わるため） |
| `dataset_hash` | 出力ファイル全部の sha256 から計算したハッシュ。学習の MLflow の run にタグとして残る |

**同じバージョン番号で作り直すと**: `input_hash` が同じならスキップ、違えば上書きせずにエラー（過去の学習結果と比べられなくなるため）。
新しいセッションを足した・変換し直した場合は、新しいバージョン番号で作ります。

## 実行手順

クラスタではワークフローから動きます（[docs/setup/04-training.md](../../docs/setup/04-training.md)）。ホストで試すとき:

```bash
cd apps/dataset-builder
uv sync
uv run pytest                                                   # ローカルのディレクトリを raw / derived / curated に見立てて動かす
set -a; source ../../.env; set +a                               # S3 の接続先とキー（port-forward 経由）
uv run dataset-builder build --version 1 --summary-file /tmp/ds.json
```

イメージのビルドと push は [04-training.md](../../docs/setup/04-training.md) 手順 4。

## 動作確認コマンド

```bash
aws s3 ls s3://curated/scene-cls/ --recursive | head
aws s3 cp s3://curated/scene-cls/v1/manifest.json - | python3 -c 'import json,sys; m=json.load(sys.stdin); print({k: (v["frames"], len(v["sessions"]), v["class_counts"]) for k, v in m["splits"].items()})'
```

期待される出力（86 セッション、組ごとに検証用 2 セッションの場合の目安）:

```
{'train': (16xxx, 74, {...}), 'val': (2xxx, 12, {'time_of_day': {'day': …, 'dusk': …, 'night': …}, 'weather': {…}})}
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `別の中身で既にあります（input_hash が違う）` | 同じバージョン番号で、入力（フレームの中身・ラベル・セッションの増減）か作り方（パラメータ・データセット作成の版）が変わった。新しいバージョン番号で作る。0.1.0 で作った v1 は、0.1.1 からは判定の仕方が変わったので別の中身扱いになる |
| `変換済みのセッションが 1 件もありません` | derived に `_manifests` が無い。先に mcap-convert を実行する |
| `組 (…) はセッションが N 本しかないので…` の警告 | その組のセッションが少なく、検証用に回せる数が減った。データを追加するか、`--val-per-combo` を減らす |
| `フレーム数がマニフェストと違います` | derived のフレームのファイルとマニフェストが食い違っている（変換が途中で落ちた等）。mcap-convert を `force=true` で作り直す |
