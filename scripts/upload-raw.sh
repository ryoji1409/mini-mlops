#!/usr/bin/env bash
# 検証に通った MCAP と metadata.json を、S3 の raw ゾーンにアップロードする。
#
# 使い方:
#   scripts/upload-raw.sh [--dry-run] <出力ディレクトリ>
#
# 置き場所（キー）は metadata.json の内容から決める:
#   s3://$RAW_BUCKET/device=<device_id>/date=<開始日(UTC)>/session_id=<session_id>/<session_id>.mcap
#   s3://$RAW_BUCKET/device=<device_id>/date=<開始日(UTC)>/session_id=<session_id>/metadata.json
#   （metadata.json に sidecars があれば、その付属ファイルも同じ場所に。フェーズ 5 の ground_truth.parquet など）
#
# 冪等性（何度実行しても同じ結果になる）の仕組み:
#   - アップロード時に、オブジェクトのユーザーメタデータ x-amz-meta-sha256 に sha256 を付ける
#   - アップロード前に head-object で既存オブジェクトを調べ、
#       同じ sha256       → スキップ（重複しない）
#       違う sha256 / なし → 上書きせずエラー（raw は「正本」なので勝手に書き換えない）
#       存在しない        → アップロード
#   - MCAP を先に、metadata.json を最後に置く。metadata.json があれば「そのセッションは全部揃っている」と判断できる
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
[[ $# -eq 1 ]] || die "使い方: $0 [--dry-run] <出力ディレクトリ>"
OUT_ROOT="$1"
[[ -d "$OUT_ROOT" ]] || die "ディレクトリがありません: $OUT_ROOT"

load_env
require_s3_env
require_cmd aws python3 "$MCAP_BIN"
RAW_BUCKET="${RAW_BUCKET:-raw}"

# 1 ファイルをアップロードする。戻り値: 0=アップロード or スキップ, 1=エラー
upload_object() {
  local file="$1" key="$2" content_type="$3"
  local sha remote err rc
  sha="$(sha256_file "$file")"
  err="$(mktemp)"

  # 既存オブジェクトの sha256 メタデータを取得する（無ければ 404 で失敗する）
  set +e
  remote="$(aws s3api head-object --bucket "$RAW_BUCKET" --key "$key" \
    --query 'Metadata.sha256' --output text 2>"$err")"
  rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    rm -f "$err"
    if [[ "$remote" == "$sha" ]]; then
      log "  スキップ（同じ sha256 がアップロード済み）: $key"
      return 0
    fi
    warn "  既に別の内容のオブジェクトがあります（上書きしません）: $key"
    warn "    ローカル sha256=$sha / S3 側 sha256=$remote"
    return 1
  fi
  if ! grep -qE 'Not Found|404' "$err"; then
    warn "  head-object が失敗しました: $key"
    cat "$err" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"

  if [[ $DRY_RUN -eq 1 ]]; then
    log "  [dry-run] アップロード予定: $file → s3://$RAW_BUCKET/$key"
    return 0
  fi
  aws s3 cp "$file" "s3://$RAW_BUCKET/$key" \
    --metadata "sha256=$sha" --content-type "$content_type" --only-show-errors

  # アップロード後に読み直して、sha256 メタデータが付いたか確認する
  remote="$(aws s3api head-object --bucket "$RAW_BUCKET" --key "$key" --query 'Metadata.sha256' --output text)"
  [[ "$remote" == "$sha" ]] || { warn "  アップロード後の確認に失敗: $key"; return 1; }
  log "  アップロード: $key"
}

# 1 セッション分（MCAP + metadata.json）を処理する
upload_session() {
  local dir="$1" meta="$1/metadata.json"
  local device session date name prefix expected_dir

  device="$(json_get "$meta" device_id)"
  session="$(json_get "$meta" session_id)"
  date="$(json_get "$meta" start_time | cut -c1-10)"  # "2026-11-04T01:32:38...Z" → "2026-11-04"
  name="$(json_get "$meta" file.name)"
  prefix="device=$device/date=$date/session_id=$session"

  # ローカルのディレクトリ構成が metadata と食い違っていたら、取り違えの可能性があるので止める
  expected_dir="$(cd "$OUT_ROOT" && pwd)/$prefix"
  [[ "$(cd "$dir" && pwd)" == "$expected_dir" ]] || {
    warn "  ディレクトリ構成が metadata.json と一致しません: ${dir}（期待: ${prefix}）"
    return 1
  }

  upload_object "$dir/$name" "$prefix/$name" application/octet-stream || return 1
  # 付属ファイルも metadata.json より先に置く（metadata.json があれば全部揃っている、という約束を守る）
  local sc_name sc_size sc_sha
  while IFS=$'\t' read -r sc_name sc_size sc_sha; do
    upload_object "$dir/$sc_name" "$prefix/$sc_name" application/octet-stream || return 1
  done < <(json_sidecars "$meta")
  upload_object "$meta" "$prefix/metadata.json" application/json || return 1
}

ok=0
ng=0
while IFS= read -r meta; do
  dir="$(dirname "$meta")"
  log "セッション: $dir"
  # 検証に通らないものはアップロードしない
  if ! "$REPO_ROOT/scripts/validate-mcap.sh" "$dir" 2>/dev/null; then
    warn "  検証 NG のためアップロードしません（詳細: scripts/validate-mcap.sh ${dir}）"
    ng=$((ng + 1))
    continue
  fi
  if upload_session "$dir"; then
    ok=$((ok + 1))
  else
    ng=$((ng + 1))
  fi
done < <(find "$OUT_ROOT" -name metadata.json -not -name '.*' | sort)

[[ $((ok + ng)) -gt 0 ]] || die "metadata.json が 1 つも見つかりません: $OUT_ROOT"
log "結果: 成功（スキップ含む）=$ok 失敗=$ng"
[[ $ng -eq 0 ]]
