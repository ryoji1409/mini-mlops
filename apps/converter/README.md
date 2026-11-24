# apps/converter — MCAP → Parquet 変換器（Ray Data）

## これは何か

raw ゾーン（`s3://raw`）の MCAP を **Ray Data の `read_mcap`** で読み、derived ゾーン（`s3://derived`）に
テレメトリの Parquet・カメラフレームのシャード・系譜マニフェストを書く変換器です。
クラスタでは Argo の WorkflowTemplate `mcap-convert`（[pipelines/](../../pipelines/README.md)）から
RayJob として実行されます（フェーズ 3 からは Kueue のキュー経由）。

| コマンド | 役割 | どこで動くか |
|---|---|---|
| `converter discover [--force] [--session-id ID] [--device-id ID]` | 未処理のセッションを数える | Argo の discover ステップ（Ray は使わない） |
| `converter convert [--force] [--session-id ID] [--device-id ID]` | Ray で変換する | RayJob の entrypoint（Ray head 上のドライバ） |
| `converter validate [--fail-on-quarantine] [--device-id ID]` | DuckDB で行数を照合する | Argo の validate ステップ |

### 出力（derived ゾーン）

```
s3://derived/
  telemetry/imu/device=<v>/date=<d>/session_id=<id>/part-00000.parquet
  telemetry/gnss/device=<v>/date=<d>/session_id=<id>/part-00000.parquet
  frames/front/device=<v>/date=<d>/session_id=<id>/part-00000.parquet   ← 32MB を超えたら part-00001 …
  _manifests/<session_id>.json     ← 系譜（= 変換完了の印）
  _quarantine/<session_id>.json    ← 変換できなかったセッションの記録
```

| データセット | 列 |
|---|---|
| imu（100Hz） | `device_id, session_id, timestamp, log_time_ns, frame_id, orientation_{x,y,z,w}, angular_velocity_{x,y,z}, linear_acceleration_{x,y,z}` |
| gnss（10Hz） | `device_id, session_id, timestamp, log_time_ns, frame_id, latitude, longitude, altitude, position_covariance(list<double>), position_covariance_type` |
| frames_front（5Hz） | `device_id, session_id, timestamp, log_time_ns, frame_index, format, width, height, jpeg(binary)` |

- `timestamp` はセンサーの時刻（`timestamp[ns, UTC]`）、`log_time_ns` は MCAP に記録された時刻（int64 ナノ秒）。
- `date` はファイルに入れていない（パスの `date=` と同名の列があると pyarrow / Ray が型の食い違いで読めなくなるため）。
- 圧縮: テレメトリは zstd。フレームは JPEG 列だけ無圧縮（既に圧縮済み）、他の列は zstd。row group は 64 フレーム。

### 冪等性と「中途半端な状態を残さない」仕組み

- **スキップ判定**: マニフェスト（または隔離記録）の「入力 MCAP の sha256」と「変換器のバージョン」が両方一致すれば、変換しない。
- **変換器のバージョン** = `pyproject.toml` の `version` = イメージタグ。コードを変えたら version を上げる（上げると全セッションが再変換される）。
- **書き込みの順番**（`session.py`）: 件数チェック → メモリ上でテーブル作成 → マニフェスト削除 → 決まった名前で上書き → 不要な古いファイルを削除 → **最後にマニフェスト**。
  途中で落ちてもマニフェストが無いので「未処理」と判定され、次回やり直される。下流は「マニフェストのあるセッションだけ読む」。
- **ファイル名は連番**（`part-00000.parquet`）。Ray の `write_parquet` は UUID 名になるので使わず、セッションごとに pyarrow で書く。
  同じ入力・同じ版なら出力はバイト単位で同じ（マニフェストの sha256 で確認できる）。

### 失敗の扱い

| 状況 | 扱い | 次回 |
|---|---|---|
| 事前チェック NG（sha256 不一致・summary が読めない・件数が metadata と違う・型が違う） | `_quarantine/` に記録（stage=preflight）、他のセッションは続ける | 同じ sha256・同じ版ならスキップ（`--force` で再挑戦） |
| read_mcap が MCAP の破損で例外 | 1 セッションずつ読み直し、壊れたセッションだけ隔離（stage=read） | 同上 |
| 変換・書き込みの例外 | 隔離（stage=convert） | 同上 |
| 処理側の問題（ワーカーのメモリ不足、受け取った件数が合わない） | **隔離しない**。`failed` として報告し、convert の終了コードを 1 にする（RayJob → ワークフローが失敗） | 未処理のままなので、再実行で変換される |

隔離があっても convert は成功扱い（終了コード 0）。validate も既定では成功（`--fail-on-quarantine` で失敗にできる）。

### 処理の流れ（`pipeline.py`）

1. discover（raw の `metadata.json` ⇔ derived の記録）
2. 事前チェックを Ray のタスクで並列実行（`preflight.py`）。MCAP 内のスキーマ（FileDescriptorSet）もここで集める
3. `ray.data.read_mcap(paths, include_paths=True, include_metadata=False)` → `groupby("path").map_groups(...)` でセッションごとに変換
   - `read_mcap` の `data` 列は protobuf の生バイト列なので、MCAP 内のスキーマからデコードする（`decode.py`。生成器のコードに依存しない）
   - groupby はソート方式（`SORT_SHUFFLE_PULL_BASED`）。既定のハッシュ方式は集約用プロセスを常駐させ、小さなワーカーでメモリ不足になった
4. 結果を集計してログに JSON で出す

## 実行手順

### ホストでテストする（クラスタ不要）

```bash
cd apps/converter
uv sync
uv run pytest            # 生成器（../mcap-generator）で短い MCAP を作り、ローカルのディレクトリを raw / derived に見立てて一通り動かす
```

### ホストから SeaweedFS に対して動かす（port-forward 経由）

```bash
kubectl -n storage port-forward svc/seaweedfs-all-in-one 8333:8333 &   # リポジトリのルートで（別ターミナルでも可）
set -a; source .env; set +a                 # admin のキー（パイプラインと同じ権限で試すなら pipeline ユーザーのキーを使う）
cd apps/converter
uv run converter discover
uv run converter convert                    # その場でローカルの Ray が立ち上がる
uv run converter validate
uv run converter discover --device-id dev-011   # デバイスで絞る（discover / convert / validate 共通。空なら全デバイス）
```

`--device-id` で絞ると、raw は `device=<id>/` の下だけを一覧し、validate も `device=<id>` の Parquet だけを数えます。
デバイスごとのワークフローが並行して動くとき、他のデバイスの書きかけのファイルを「残骸」と誤判定しないためです（フェーズ 3）。

### イメージのビルドと push

GHCR への push の手順は [docs/setup/02-pipeline.md](../../docs/setup/02-pipeline.md) 手順 6。ローカルでビルドだけするなら:

```bash
docker buildx build --build-arg CONVERTER_VERSION=0.2.1 --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) \
  --tag mini-mlops-converter:0.2.1-local --load apps/converter       # リポジトリのルートで
```

- タグ = `pyproject.toml` の version。コードを変えたら version と `pipelines/mcap-convert.yaml` のタグを上げる（テストで一致を確かめている）。

- ベース: `rayproject/ray:2.59.1-py312-cpu`（arm64 / amd64）。Ray・pyarrow・numpy・pandas・protobuf はベースの版と `uv.lock` の版を揃えてあり、入れ替わらない
  （requests / click などの小さな依存はロックの版に上がる）。
- DuckDB の httpfs 拡張はビルド時に入れる（実行時にインターネットへ取りに行かない）。

## 動作確認コマンド

```bash
cd apps/converter && uv run pytest -q
```

期待される出力: `10 passed`

```bash
# クラスタで変換したあと（port-forward と .env が必要）
set -a; source .env; set +a
aws s3 ls s3://derived/_manifests/
aws s3 cp s3://derived/_manifests/<session_id>.json - | python3 -m json.tool | head -40
```

マニフェストの例（抜粋）:

```json
{
  "status": "converted",
  "session_id": "dev-001-20261104T013238Z",
  "converter": {"name": "converter", "version": "0.2.1", "image": "ghcr.io/<user>/mini-mlops-converter:0.2.1", "ray_version": "2.59.1", ...},
  "input": {"mcap": "s3://raw/device=dev-001/.../dev-001-20261104T013238Z.mcap", "sha256": "817c7959...", ...},
  "outputs": {"imu": {"rows": 3000, "files": [{"path": "s3://derived/telemetry/imu/.../part-00000.parquet", "rows": 3000, "size_bytes": ..., "sha256": "..."}]}, ...},
  "topic_row_counts": {"/camera/front/image_compressed": 150, "/gnss": 300, "/imu": 3000}
}
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `pyarrow.lib.ArrowTypeError: Unable to merge: Field session_id has incompatible types: string vs dictionary<...>` | `pyarrow.parquet.read_table` は既定でパスの `session_id=` を dictionary 型の列として足すため、ファイル内の `session_id` 列とぶつかる。`pq.read_table(path, partitioning=None)` で読むか、`pyarrow.dataset.dataset(path, partitioning="hive")` / DuckDB / Ray Data を使う |
| ローカルで `ValueError: After taking into account object store and redis memory usage, the amount of memory on this node available for tasks and actors (0.02 GB) ...` | メモリの小さいコンテナで `ray.init()` / `ray start` したとき、Ray が「今の空きメモリ」から計算して足りないと判断した。`ray start --memory=<limit のバイト数>` を付ける（KubeRay はコンテナの limit から自動で付けるので、クラスタでは起きない） |
| `... worker(s) were killed due to the node running low on memory` → 行数が metadata と合わない | ワーカーがメモリ不足で落ち、タスクの再実行で行が重複した（実際に起きた）。変換器は件数を確かめてから書くので、この場合は何も書かずに `failed` になる。ワーカーのメモリを増やすか、同時に動くセッションを減らす |
| `事前チェック NG: RecordLengthLimitExceeded: unknown (opcode ...)` | MCAP が途中で切れている（summary が無い）。`_quarantine/<session_id>.json` に記録される。正しいファイルを raw に置き直し、`--force`（または `--session-id <id> --force`）で再変換 |
| `事前チェック NG: sha256 が metadata.json と違います` | raw の MCAP と metadata.json の組み合わせが違う / 転送中に壊れた。ホストで `scripts/validate-mcap.sh` からやり直す |
| `converter validate` が `ORPHAN` を出す | マニフェストの無い Parquet が残っている（変換が途中で落ちた、変換器の版を上げたあと失敗した等）。`converter convert` を再実行すると片付く |
| `duckdb.IOException: ... HTTP 403` | DuckDB の S3 キーが違う。Pod なら Secret `pipeline-s3-credentials`、ホストなら `.env` を確認 |
| `raw にセッションが 1 件も無いデバイスが指定されました: …`（終了コード 1） | `--device-id` に raw に無いデバイスを指定した（打ち間違いなど）。`aws s3 ls s3://raw/` で実在するデバイスを確認する。黙って「対象 0 件で成功」にしないよう、0.2.1 からエラーにしている |
