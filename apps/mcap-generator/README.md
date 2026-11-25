# mcap-generator — 合成 MCAP 生成器

## これは何か

ホスト（クラスタの外の作業用マシン）で実行して、**1 セッション（session）につき 1 本の MCAP** と `metadata.json` を作るツールです。
本物のセンサーログの代わりに、プログラムで描いた抽象的なシーン（格子模様の床・標識・立方体・円柱）とそれっぽいセンサー値を入れます。

| トピック | スキーマ | 周波数 | 中身 |
|---|---|---|---|
| `/camera/front/image_compressed` | `foxglove.CompressedImage` | 5Hz | 640x360 の JPEG（Pillow で描いた抽象シーン） |
| `/imu` | `mini_mlops.Imu`（独自。中身は Foxglove の Quaternion / Vector3） | 100Hz | 姿勢・角速度・加速度 |
| `/gnss` | `foxglove.LocationFix` | 10Hz | 架空のコースを動く緯度経度 |

- チャンク圧縮は **zstd**。Foxglove でそのまま開けます。
- セッションごとに **時間帯（day / dusk / night）× 天候（clear / rain）** と、大きな物体（立方体 0〜3 個）・小さな物体（円柱 0〜3 個）の数を変えます。
  時間帯と天候の 6 通りの組み合わせは偏りが出ないよう 1 周ずつ順番に使うので、6 セッションなら全組み合わせが 1 回ずつ出ます。
- **同じ `--seed` なら、バイト単位で同じファイルができます**（sha256 も同じ）。アップロードの冪等性の確認に使えます。

### `--video`（フェーズ 5）

`--video` を付けると、次が増えます（付けなければ出力は以前と 1 バイトも変わりません）。設計の理由は [docs/architecture.md](../../docs/architecture.md) の「フェーズ 5」。

| 追加されるもの | 中身 |
|---|---|
| `/camera/front/compressed_video`（`foxglove.CompressedVideo`、10Hz） | H.265（320×180、x265 で CPU エンコード）。GOP = 10 フレーム = 1 秒、B フレームなし、各 IDR に VPS/SPS/PPS、Annex B、1 メッセージ = 1 フレーム |
| `/navigation/route`（`foxglove.LocationFixes`、先頭に 1 回） | 予定ルート。真の軌跡を 5m 間隔で間引き、終わりから 60m 先まで延ばしたもの（`/gnss` のコースと必ず整合する） |
| MCAP のチャンクの区切り方 | GOP の先頭で必ずチャンクを閉じる → 1 チャンク = 1 GOP。予定ルートは先頭の小さなチャンクに単独で入る（60 秒ならチャンク 61 個） |
| `ground_truth.parquet`（MCAP と同じディレクトリ） | 動画のフレームごとの正解値: `segment_id`（10 秒ごと、`<session_id>_000`〜）、`gop_index`、`frame_in_gop`、時間帯・天候、近く（30m 以内）の小物体の数、進路上・前方 40m 以内の大物体の数 |
| `metadata.json` | `schema_version: 2`。`video`・`route`・`segments`・`sidecars`（付属ファイルの名前・サイズ・sha256）が増える |

### 出力

S3 の raw ゾーンと同じパーティション構成で出力します（開始日は UTC）。

```
output/
└── device=dev-001/
    └── date=2026-11-04/
        └── session_id=dev-001-20261104T013238Z/
            ├── dev-001-20261104T013238Z.mcap
            └── metadata.json
```

`metadata.json` の例（`conditions` は後のフェーズで「正解ラベル」として使う）:

```json
{
  "schema_version": 1,
  "device_id": "dev-001",
  "session_id": "dev-001-20261104T013238Z",
  "start_time": "2026-11-04T01:32:38.000000000Z",
  "end_time": "2026-11-04T01:33:07.990000000Z",
  "start_time_ns": 1793755958000000000,
  "end_time_ns": 1793755987990000000,
  "duration_s": 30,
  "conditions": { "time_of_day": "day", "weather": "rain", "num_large_objects": 1, "num_small_objects": 3 },
  "topics": [
    { "topic": "/camera/front/image_compressed", "schema": "foxglove.CompressedImage", "message_encoding": "protobuf", "rate_hz": 5, "message_count": 150 },
    { "topic": "/imu", "schema": "mini_mlops.Imu", "message_encoding": "protobuf", "rate_hz": 100, "message_count": 3000 },
    { "topic": "/gnss", "schema": "foxglove.LocationFix", "message_encoding": "protobuf", "rate_hz": 10, "message_count": 300 }
  ],
  "camera": { "width": 640, "height": 360, "format": "jpeg" },
  "file": { "name": "dev-001-20261104T013238Z.mcap", "size_bytes": 2638057, "sha256": "817c7959...", "compression": "zstd" },
  "generator": { "name": "mcap-generator", "version": "0.1.0", "seed": 42, "session_seed": 1194819984 }
}
```

- `end_time` は **最後のメッセージの時刻**（開始 + 記録秒数 − 10ms）。`mcap info` の `end` と一致します。
- 時間帯は日本時間（JST）で決めています（例: night = JST 19〜23 時開始）。タイムスタンプとパーティションは UTC です。

## 実行手順

前提: [uv](https://docs.astral.sh/uv/) がインストール済み。Python 3.12 は uv が自動で用意します。

```bash
cd apps/mcap-generator
uv sync                                   # 依存関係をインストール（uv.lock どおりの版）
uv run mcap-generate --devices 2 --sessions-per-device 3 --out ../../output --seed 42
# フェーズ 3 の並行実行の確認用: dev-011〜dev-014 を 4 台 × 5 セッション追加（既存の dev-001 / dev-002 とは重ならない）
uv run mcap-generate --first-device 11 --devices 4 --sessions-per-device 5 --out ../../output --seed 7
```

| 引数 | 既定値 | 意味 |
|---|---|---|
| `--devices` | 2 | デバイス数 |
| `--sessions-per-device` | 3 | 1 台あたりのセッション数 |
| `--first-device` | 1 | デバイス番号の始まり（1 → dev-001）。既存のデータと ID が重ならないデータを追加で作るときにずらす |
| `--out` | `./output` | 出力先 |
| `--seed` | 42 | 乱数シード |
| `--min-duration` / `--max-duration` | 30 / 60 | 記録時間 [秒] の範囲 |
| `--start-date` | 2026-11-01 | 記録日の起点。ここから 14 日間に記録日をばらまく |

## 動作確認コマンド

```bash
uv run pytest -q                                    # 期待: 11 passed（--video の 4 件を含む）
uv run mcap-generate --out ../../output --seed 42   # 期待: [1/6]〜[6/6] が表示される
mcap info ../../output/device=dev-001/date=*/session_id=*/*.mcap | head -20
```

期待される出力（`--seed 42` の場合、1 本目）:

```
[1/6] dev-001-20261104T070847Z dusk/rain 大0 小3 33s 2.8MB (/camera/front/image_compressed=165, /imu=3300, /gnss=330) 0.7s
```

`mcap info` の channels 欄に 3 トピックが出て、件数が `metadata.json` と同じなら OK です。
（照合は `scripts/validate-mcap.sh` が自動でやります）

Foxglove で見る場合: Foxglove を開き、MCAP ファイルをドラッグ＆ドロップ → Image パネルで `/camera/front/image_compressed`、
Map パネルで `/gnss`、Plot パネルで `/imu.linear_acceleration.x` などを表示できます。

`--video` の確認（テストでも確かめている）:

```bash
uv run mcap-generate --video --first-device 31 --devices 1 --sessions-per-device 1 --min-duration 30 --max-duration 30 --out /tmp/gen-video --seed 31
mcap info /tmp/gen-video/device=dev-031/*/*/*.mcap | grep -E "zstd|compressed_video|route"
# zstd: [31/31 chunks] …（ルート用 1 + GOP 30）
```

## コードの構成

| ファイル | 役割 |
|---|---|
| `src/mcap_generator/cli.py` | コマンドライン引数の処理 |
| `src/mcap_generator/scenario.py` | セッションごとの条件（時間帯・天候・物体の数など）を乱数シードから決める |
| `src/mcap_generator/sensors.py` | デバイスの軌跡を計算し、IMU / GNSS の値を作る |
| `src/mcap_generator/scene.py` | Pillow で抽象シーンを描く |
| `src/mcap_generator/imu_schema.py` | 独自スキーマ `mini_mlops.Imu` を定義する |
| `src/mcap_generator/writer.py` | MCAP と metadata.json を書き出す（`--video` のときは GOP の先頭でチャンクを閉じる） |
| `src/mcap_generator/video.py` | H.265 のエンコード（PyAV + libx265） |
| `src/mcap_generator/route.py` | 予定ルートを軌跡から作る |
| `src/mcap_generator/ground_truth.py` | フレームごとの正解値とセグメント ID、`ground_truth.parquet` の書き出し |

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `uv: command not found` | uv をインストールする（https://docs.astral.sh/uv/） |
| `No interpreter found for Python >=3.12, <3.13` | uv が Python をダウンロードできていない。ネットワークを確認して `uv python install 3.12` |
| 同じ seed なのに sha256 が変わった | 依存ライブラリの版が変わると、JPEG や zstd の出力バイトが変わることがある。`uv sync` で uv.lock どおりの版に戻す |
| `RuntimeError: フレーム N の圧縮データが 0 個出てきました` | x265 が低遅延の設定になっていない（PyAV / FFmpeg の版が違う）。`uv sync` で uv.lock どおりの版にする |
| 出力先に `.xxx.mcap.tmp` が残っている | 生成途中で中断した。消して再実行してよい（完成したファイルは一時ファイルからの改名で作るので、中途半端な `.mcap` は残らない） |
