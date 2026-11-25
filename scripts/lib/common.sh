# validate-mcap.sh / upload-raw.sh から `source` して使う共通関数。単体では実行しない。
#
# - リポジトリ直下の .env を読み込む（S3 のエンドポイントやキー）
# - 必要なコマンドがあるか確認する
# - sha256 を計算する（OS によってコマンド名が sha256sum / shasum と違うのを吸収する）

# リポジトリのルート（scripts/lib/ の 2 つ上）
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# mcap CLI のパス。別の場所のバイナリを使いたいときは MCAP_BIN=/path/to/mcap で上書きできる
MCAP_BIN="${MCAP_BIN:-mcap}"

log()  { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
warn() { printf '\033[33m[警告]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[エラー]\033[0m %s\n' "$*" >&2; exit 1; }

# .env を読み込む。中の変数はすべて export される（aws CLI などの子プロセスに渡すため）
load_env() {
  local env_file="${ENV_FILE:-$REPO_ROOT/.env}"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  else
    warn "$env_file がありません。cp .env.example .env で作成してください（docs/setup/01-storage.md 手順 1）（環境変数が設定済みならこのまま続行します）"
  fi
}

# S3 に接続するのに必要な環境変数が揃っているか確認する
require_s3_env() {
  local v
  for v in AWS_ENDPOINT_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION; do
    [[ -n "${!v:-}" ]] || die "環境変数 $v が設定されていません（.env を確認してください）"
  done
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "コマンド '$c' が見つかりません。README の「事前準備」を参照してインストールしてください"
  done
}

# ファイルの sha256（16 進文字列）を標準出力に出す
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# JSON ファイルから値を 1 つ取り出す（jq を必須にしないため python3 の標準ライブラリを使う）
#   例: json_get metadata.json device_id
#       json_get metadata.json file.sha256
json_get() {
  python3 -I -c '
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)
' "$1" "$2"
}

# metadata.json の sidecars（MCAP と一緒に置く付属ファイル。フェーズ 5 の ground_truth.parquet など）を
# 「名前 TAB サイズ TAB sha256」の形で 1 行ずつ出す。無ければ何も出さない
json_sidecars() {
  python3 -I -c '
import json, sys
for s in json.load(open(sys.argv[1])).get("sidecars", []):
    print(s["name"], s["size_bytes"], s["sha256"], sep="\t")
' "$1"
}
