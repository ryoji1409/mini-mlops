#!/usr/bin/env bash
# アップロード前の MCAP 検証。
#
# 使い方:
#   scripts/validate-mcap.sh <出力ディレクトリ または セッションディレクトリ> [...]
#
# metadata.json がある各ディレクトリ（= 1 セッション）について、次をすべて確認する:
#   1. metadata.json に書かれた MCAP ファイルが存在し、サイズと sha256 が一致する（転送中の破損・取り違えの検出）
#   2. `mcap doctor` で構造エラーがない（途中で切れたファイルなどを検出）
#   3. `mcap info` のトピック・メッセージ数・開始/終了時刻が metadata.json と一致する
#   4. 付属ファイル（metadata.json の sidecars。フェーズ 5 の ground_truth.parquet など）があれば、サイズと sha256 が一致する
# 1 つでも失敗したセッションがあれば終了コード 1 を返す。
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

[[ $# -ge 1 ]] || die "使い方: $0 <出力ディレクトリ または セッションディレクトリ> [...]"
require_cmd "$MCAP_BIN" python3

CHECK_INFO="$REPO_ROOT/scripts/lib/check_mcap_info.py"

# 1 セッション分を検証する。OK なら 0、NG なら 1 を返す
validate_session() {
  local dir="$1" meta="$1/metadata.json"
  local name mcap expected_sha expected_size actual_sha actual_size

  name="$(json_get "$meta" file.name)" || { warn "$meta を読めません"; return 1; }
  mcap="$dir/$name"
  [[ -f "$mcap" ]] || { warn "$mcap がありません"; return 1; }

  # 1. サイズと sha256
  expected_size="$(json_get "$meta" file.size_bytes)"
  actual_size="$(wc -c <"$mcap" | tr -d ' ')"
  [[ "$expected_size" == "$actual_size" ]] || { warn "サイズ不一致: metadata=$expected_size 実際=$actual_size"; return 1; }
  expected_sha="$(json_get "$meta" file.sha256)"
  actual_sha="$(sha256_file "$mcap")"
  [[ "$expected_sha" == "$actual_sha" ]] || { warn "sha256 不一致: metadata=$expected_sha 実際=$actual_sha"; return 1; }

  # 2. mcap doctor（エラーがあると 0 以外で終わる）
  if ! "$MCAP_BIN" doctor "$mcap" >/dev/null; then
    warn "mcap doctor でエラー: $mcap"
    return 1
  fi

  # 3. mcap info と metadata.json の突き合わせ（時刻はナノ秒の整数で出させて比較する）
  local info
  info="$("$MCAP_BIN" --time-format ns info "$mcap")" || { warn "mcap info が失敗: $mcap"; return 1; }
  printf '%s\n' "$info" | python3 -I "$CHECK_INFO" "$meta" || return 1

  # 4. 付属ファイル
  local sc_name sc_size sc_sha
  while IFS=$'\t' read -r sc_name sc_size sc_sha; do
    [[ -f "$dir/$sc_name" ]] || { warn "付属ファイルがありません: $dir/$sc_name"; return 1; }
    [[ "$(wc -c <"$dir/$sc_name" | tr -d ' ')" == "$sc_size" ]] || { warn "付属ファイルのサイズ不一致: $sc_name"; return 1; }
    [[ "$(sha256_file "$dir/$sc_name")" == "$sc_sha" ]] || { warn "付属ファイルの sha256 不一致: $sc_name"; return 1; }
  done < <(json_sidecars "$meta")
  return 0
}

ok=0
ng=0
# 引数の各パスの下から metadata.json を探す（ドット始まりの一時ファイルは除外）
while IFS= read -r meta; do
  dir="$(dirname "$meta")"
  log "検証: $dir"
  if validate_session "$dir"; then
    ok=$((ok + 1))
  else
    printf '  → \033[31mNG\033[0m %s\n' "$dir" >&2
    ng=$((ng + 1))
  fi
done < <(find "$@" -name metadata.json -not -name '.*' | sort)

[[ $((ok + ng)) -gt 0 ]] || die "metadata.json が 1 つも見つかりません: $*"
log "結果: OK=$ok NG=$ng"
[[ $ng -eq 0 ]]
