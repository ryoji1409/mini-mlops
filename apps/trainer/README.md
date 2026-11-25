# apps/trainer — シーン分類の分散学習（PyTorch CPU + DDP）

## これは何か

curated の学習用データセット（[apps/dataset-builder](../dataset-builder/README.md)）で、画像から **時間帯（3 クラス）と天候（2 クラス）** を当てる
小さな CNN（約 7 万パラメータ、事前学習の重みなし）を、CPU の分散学習（PyTorch DDP、通信は gloo）で学習します。
同じ学習のコードを、2 つの方式から起動できます。

| コマンド | 起動のされ方 | データの分担 | 落ちたときの再開 |
|---|---|---|---|
| `trainer train-torchrun` | Kubeflow Trainer の TrainJob → JobSet → 各 Pod で `torchrun --no-python trainer train-torchrun` | 各 rank が別々の Parquet ファイルを読む | JobSet が全ワーカーを作り直し、コードが `s3://curated/checkpoints/<ジョブ名>/` から再開 |
| `trainer train-ray` | KubeRay の RayJob → head の上のドライバが Ray Train の TorchTrainer を起動 | Ray Data が行をワーカーに均等に配る | Ray Train が `s3://curated/checkpoints/ray/<ジョブ名>/` から自動で再開 |
| `trainer register` | ワークフローの最後のステップ | — | — |
| `trainer trace` | ホストから手で | — | — |
| `trainer wait-trainjob` | ワークフローのステップ（TrainJob の完了待ち） | — | — |
| `trainer bench-video` | ホストから手で（フェーズ 5） | 動画ローダーを 1 エポック回し、samples/sec と S3 から読んだバイト数を測る。PNG も保存 | — |
| `trainer decode-gop` | ホストから手で（フェーズ 5） | 索引の参照位置だけを Range GET して 1 GOP をデコードしてみる | — |

| ファイル | 役割 |
|---|---|
| `model.py` | 小さな CNN（畳み込み 4 段 + 平均プーリング）と、出力ヘッド 2 つ |
| `data.py` | データセットの読み込み、rank ごとのファイルの分担、JPEG のデコード、バッチ作り |
| `engine.py` | 1 エポックの学習と評価（2 つのヘッドの交差エントロピーの和。混同行列で集計） |
| `torchrun_train.py` / `ray_train.py` | 2 つの起動方式。全 rank のバッチ数を揃える、評価を全 rank で足し合わせる、チェックポイント |
| `checkpoint.py` | torchrun 版のチェックポイント（エポックごと。`latest.json` が最新を指す） |
| `tracking.py` | MLflow への記録（rank 0 だけ）。パラメータ、エポックごとのメトリクス、混同行列の画像、モデル、系譜のタグ |
| `register.py` | 検証の正解率（2 つのヘッドの低いほう）が閾値を超えたら、`scene-classifier` に登録してエイリアス `candidate` を付ける |
| `trace.py` | モデル → run → データセットのマニフェスト → derived のマニフェスト → raw の MCAP をハッシュを照合しながらたどる |
| `wait.py` | TrainJob の `status.conditions` を見張る（Argo の成功条件ではリストの中を判定できないため） |
| `video_dataset.py` | 動画のサンプル一覧（[apps/indexer](../indexer/README.md) の select が作る）を読む IterableDataset。GOP ごとに Range GET → デコード → 経路ラスタ（[apps/navi-raster](../navi-raster/README.md)）。先読みのスレッド数（`prefetch`）と人工的な遅延（`latency_ms`）を指定できる |
| `video_decoder.py` | デコーダのインターフェース（`VideoDecoder`）と PyAV の実装。GPU のある環境では NVDEC の実装に差し替える |
| `bench_video.py` / `video_check.py` | `bench-video` / `decode-gop` |

0.2.0 から、同じリポジトリのライブラリ（`apps/indexer`・`apps/navi-raster`）をパスで参照しています（`pyproject.toml` の `[tool.uv.sources]`）。
そのため **イメージのビルドの起点は `apps/`** です（`docker buildx build -f apps/trainer/Dockerfile apps`。除外は `Dockerfile.dockerignore`）。

### CPU の使いすぎを防ぐ

Pod の CPU limit は 1 です。PyTorch は何もしないとノードのコア数ぶんのスレッドを作り、CPU を取り合ってかえって遅くなるので、
`OMP_NUM_THREADS=1`（Pod の環境変数）に合わせて `torch.set_num_threads` を設定しています。

### MLflow への記録

rank 0 だけが記録します（全 rank が記録すると、同じ値が world_size 回書かれる）。成果物（画像・モデル）は MLflow サーバー経由で `s3://mlflow` に保存されるので、
学習の Pod は `s3://mlflow` のキーを持ちません（`MLFLOW_TRACKING_URI` だけ）。
チェックポイントに MLflow の run_id を入れてあり、再開したときは同じ run に記録を続けます。

モデルは `mlflow.pytorch.log_model(..., serialization_format="pickle")` で保存します（MLflow 3.18 の既定の `pt2` は、
出力が 2 つのこのモデルでは入出力の型の宣言が手間なので使わない）。読み込むときは `trainer` パッケージが必要です。

### 系譜のタグ（run とモデルのバージョンの両方に付ける）

`dataset.version`・`dataset.manifest`・`dataset.hash`・`converter.versions`・`trainer.image`・`trainer.version`・`mini-mlops.engine`・`mini-mlops.job_name`

## 実行手順

クラスタではワークフロー `train-scene-classifier` から動きます（[docs/setup/04-training.md](../../docs/setup/04-training.md)）。ホストでテストする:

```bash
cd apps/trainer
uv sync
uv run pytest            # ローカルの CPU で、本当に 2 プロセスの DDP（torchrun）と 2 ワーカーの Ray Train を動かす（1〜2 分）
```

torch は、Linux では CPU 専用の配布元（`download.pytorch.org/whl/cpu`）から、それ以外では PyPI から入ります（`pyproject.toml` の `[tool.uv.sources]`）。
PyPI の Linux 版 torch は arm64 でも CUDA のライブラリ（数 GB）を一緒に引き込むためです。イメージのビルドと push は [04-training.md](../../docs/setup/04-training.md) 手順 4。

## 動作確認コマンド

```bash
uv run pytest -q                                                                        # 期待: 7 passed（動画ローダーの 3 件を含む。生成器を uv で別に動かす）
MLFLOW_TRACKING_URI=http://localhost:5050 uv run trainer trace --limit 3               # 系譜（04-training.md 手順 11）
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| RayJob で学習ワーカーが `OOM kill reason` で強制終了される | 学習ワーカー 2 つが同じ worker の Pod に入った（worker は CPU 2 つと名乗っているので、置き方を指定しないとまとめて置かれる）。`--placement-strategy=STRICT_SPREAD`（ワークフローで指定済み） |
| ホストのテストで torchrun が止まる（`The IPv6 network addresses of (…ip6.arpa) cannot be retrieved`） | 環境によっては、torchrun の `--standalone` が自分のホスト名を引けずに止まる。テストは `--master-addr=127.0.0.1` で動かしている |
| `ImportError: cannot import name 'HTTPFileSystem' from 'fsspec.implementations.http'` | Ray Data が使う aiohttp が入っていない。依存に `aiohttp`（Ray イメージと同じ版）を入れてある |
| `MlflowException: If serialization_format is set to 'pt2', then input_example is required` | MLflow 3.18 の既定の保存形式。`serialization_format="pickle"` で保存している |
| 学習が epoch の途中で止まる | DDP で rank ごとのバッチ数がずれると、多い rank が all-reduce で待ち続ける。torchrun 版は全 rank の最小に揃え、Ray 版は `drop_last=True` で揃えている |
| `ワーカー数（N）がファイル数（M）より多い` | データセットの学習用ファイル数（既定 8）より多いワーカーでは分担できない |
| ホストで `uv run` の中から Ray を起動すると、ワーカーが `Failed to generate package metadata for indexer @ directory+../indexer` で起動しない | Ray は `uv run` を検知すると、ワーカーでも作業ディレクトリを一時的な場所に複製して uv run をやり直す。複製先では `../indexer` が見つからない。`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` を付ける（テストは設定済み。イメージの中では uv run を使わないので起きない） |
| `bench-video` で `AccessDenied` | training ユーザーに raw / derived の読み取りがない（[05-video-index.md](../../docs/setup/05-video-index.md) 手順 2） |
