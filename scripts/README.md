# scripts — 手作業では難しい処理だけのスクリプト

## これは何か

構築・確認の手順は `docs/setup/` の手順書に手で実行するコマンドとして書いてあります。
ここには、セッション（ファイル）ごとのループや照合があって、手で打つのが現実的でない処理だけを置いています。
ホスト（クラスタの外の作業用マシン）で、リポジトリのルートから実行します。S3 の接続情報はルートの `.env` から読みます（aws CLI 標準の環境変数）。

| スクリプト | 役割 | 使う手順 |
|---|---|---|
| `validate-mcap.sh` | アップロード前の検証。セッションごとに、metadata.json と MCAP の sha256・サイズ、`mcap doctor`（構造チェック）、`mcap info` と metadata.json の一致を確かめる | [01-storage.md](../docs/setup/01-storage.md) 手順 7・9 |
| `upload-raw.sh` | 検証を通ったセッションだけを raw ゾーンにアップロードする（sha256 で重複・上書きを防ぐ。`--dry-run` あり） | [01-storage.md](../docs/setup/01-storage.md) 手順 8 |
| `lib/make_broken_session.py` | 正常なセッションから「途中で切れた MCAP」のセッションを作る（隔離の確認と、変換器のテストで使う） | [02-pipeline.md](../docs/setup/02-pipeline.md) 手順 10 |
| `lib/common.sh` | 上の 2 つのシェルスクリプトの共通関数（.env の読み込み、sha256、JSON の値の取り出し） | — |
| `lib/check_mcap_info.py` | `mcap info` の出力と metadata.json の突き合わせ（Python 標準ライブラリのみ） | — |

### アップロードの冪等性（`upload-raw.sh`）

各オブジェクトにユーザーメタデータ `x-amz-meta-sha256` を付けてアップロードします。
アップロード前に `head-object` で既存オブジェクトを調べ、

| S3 側の状態 | 動作 |
|---|---|
| 存在しない | アップロード（後で sha256 メタデータが付いたか読み直して確認） |
| 同じ sha256 | スキップ（何度実行しても重複しない） |
| 違う sha256 / sha256 なし | **上書きせずエラー**（raw は正本なので、勝手に書き換えない） |

MCAP を先に、`metadata.json` を最後に置くので、「metadata.json があればそのセッションは揃っている」と判断できます。
置き場所（キー）は metadata.json の内容から決めます: `s3://raw/device=<device_id>/date=<開始日(UTC)>/session_id=<session_id>/`

## 実行手順

```bash
set -a; source .env; set +a                # 接続先とキー（validate-mcap.sh は S3 を使わない）
scripts/validate-mcap.sh output            # output/ 以下の全セッション（セッションのディレクトリを直接渡してもよい）
scripts/upload-raw.sh --dry-run output     # アップロードの予定だけ表示
scripts/upload-raw.sh output
```

## 動作確認コマンド

```bash
scripts/validate-mcap.sh output            # 最後に「結果: OK=6 NG=0」
scripts/upload-raw.sh output               # 2 回目: すべて「スキップ（同じ sha256 がアップロード済み）」
aws s3 ls s3://raw/ --recursive            # 12 行（6 セッション × MCAP と metadata.json）
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `コマンド 'mcap' が見つかりません` | Foxglove の mcap CLI（v0.4.0）をインストールする。別の場所のバイナリを使うなら `MCAP_BIN=/path/to/mcap scripts/...` |
| `Could not connect to the endpoint URL: "http://localhost:8333/..."` | S3 API の port-forward が動いていない（[01-storage.md](../docs/setup/01-storage.md) 手順 4） |
| `InvalidAccessKeyId` / `SignatureDoesNotMatch` | `.env` と SeaweedFS の Secret のキーが違う（[01-storage.md](../docs/setup/01-storage.md) 手順 2 をやり直して SeaweedFS を再起動） |
| アップロードで `XAmzContentSHA256Mismatch` や checksum 関連のエラー | 新しい aws CLI が付ける CRC 系チェックサムに S3 互換側が未対応。`.env` に `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` と `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` があるか確認（`.env.example` には記載済み） |
| `既に別の内容のオブジェクトがあります（上書きしません）` | 同じセッション ID で中身の違うファイルを上げようとした（seed や生成器の版を変えて再生成した等）。raw は正本なので自動では上書きしない。本当に置き換えるなら、内容を確認したうえで手動で該当オブジェクトを削除する |
| `検証 NG のためアップロードしません` | `scripts/validate-mcap.sh <そのディレクトリ>` を実行して詳細を確認。途中で切れたファイルなら再生成する |
| `mcap doctor` は通るのに `mcap info` の照合が NG | `metadata.json` と MCAP の組み合わせが違う（別のセッションのものと取り違え）か、生成が途中で止まった。再生成する |
| `...: unbound variable`（変数名の後ろに文字化けが付く） | 古い bash（3.2 など）は、`$var（` のように変数の直後に全角文字があると変数名の一部と解釈する。スクリプトを編集するときは `${var}（` と波括弧で囲む |
