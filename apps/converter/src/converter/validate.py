"""DuckDB による検証（validate コマンド）。

derived の Parquet を DuckDB で実際に数え、セッションごと・トピックごとの行数が
raw の metadata.json の message_count と一致するかを確認する。
マニフェストの数字ではなく、Parquet そのものを数えるのがポイント（書き込みの取りこぼしも見つかる）。

セッションごとの判定:
    OK          … マニフェストがあり（入力 sha256・変換器の版が一致）、全データセットの行数が一致
    QUARANTINED … 隔離されている（ワークフローは成功扱い。--fail-on-quarantine で失敗にできる）
    MISMATCH    … 行数が一致しない                → 失敗
    MISSING     … 変換も隔離もされていない        → 失敗
さらに、マニフェストの無いセッションの Parquet（途中で落ちた残骸など）が残っていたり、
Parquet として読めないファイルが混ざっていたりしても失敗にする。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from converter.config import S3Settings, Settings
from converter.discover import check_devices_exist, list_raw_sessions
from converter.layout import DATASETS, SessionRef, manifest_key, quarantine_key
from converter.records import matches
from converter.storage import open_location, read_json

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionCheck:
    session_id: str
    status: str  # OK / QUARANTINED / MISMATCH / MISSING
    detail: str


def _sql_str(value: str) -> str:
    """SQL の文字列リテラルにする（' を '' にエスケープ）。"""
    return "'" + value.replace("'", "''") + "'"


def connect(s3: S3Settings, use_s3: bool = True) -> duckdb.DuckDBPyConnection:
    """DuckDB に接続し、S3 互換ストレージを読めるように設定する。

    httpfs 拡張はコンテナイメージの作成時に入れてある（実行時にインターネットから取らない）。
    use_s3=False（テストでローカルのディレクトリを読むとき）は S3 の設定をしない。
    """
    con = duckdb.connect()
    if not use_s3:
        return con
    con.execute("INSTALL httpfs")  # 入っていれば何もしない
    con.execute("LOAD httpfs")
    opts = [f"REGION {_sql_str(s3.region)}", "URL_STYLE 'path'"]  # SeaweedFS はパス形式のアドレスで使う
    if s3.access_key and s3.secret_key:
        opts += [f"KEY_ID {_sql_str(s3.access_key)}", f"SECRET {_sql_str(s3.secret_key)}"]
    if s3.endpoint_url:
        scheme, _, host = s3.endpoint_url.partition("://")
        opts += [f"ENDPOINT {_sql_str(host)}", f"USE_SSL {'true' if scheme == 'https' else 'false'}"]
    con.execute(f"CREATE SECRET s3_derived (TYPE s3, {', '.join(opts)})")
    return con


def count_rows(con: duckdb.DuckDBPyConnection, derived_uri: str, prefix: str, device_id: str | None = None) -> dict[str, int]:
    """あるデータセットの Parquet を全部読み、session_id ごとの行数を返す。

    hive_partitioning は使わず、ファイルの中の session_id 列で数える
    （パスの session_id= とファイル内の列が同じ名前なので、二重に扱わないようにする）。
    device_id を渡すと、パスの device=<id> の下だけを読む（他のデバイスのファイルは数えない）。
    """
    device_dir = f"device={device_id}" if device_id else "*"
    pattern = f"{derived_uri}/{prefix}/{device_dir}/*/*/*.parquet"
    query = f"SELECT session_id, count(*) FROM read_parquet({_sql_str(pattern)}, hive_partitioning = false) GROUP BY session_id"
    try:
        return {session_id: int(n) for session_id, n in con.execute(query).fetchall()}
    except duckdb.IOException as e:
        if "No files found" in str(e):
            return {}  # まだ 1 ファイルも無い
        raise


def _check_session(ref: SessionRef, manifest: dict[str, Any] | None, quarantine: dict[str, Any] | None, version: str, counts: dict[str, dict[str, int]]) -> SessionCheck:
    """1 セッション分の判定。"""
    if matches(manifest, ref, version):
        expected = ref.expected()
        diffs = []
        for topic, spec in DATASETS.items():
            want = expected.get(topic, 0)
            got = counts[spec.name].get(ref.session_id, 0)
            in_manifest = manifest["outputs"].get(spec.name, {}).get("rows", 0)  # type: ignore[index]
            if not (want == got == in_manifest):
                diffs.append(f"{spec.name}: metadata={want} parquet={got} manifest={in_manifest}")
        if diffs:
            return SessionCheck(ref.session_id, "MISMATCH", "; ".join(diffs))
        detail = ", ".join(f"{s.name}={counts[s.name].get(ref.session_id, 0)}" for s in DATASETS.values())
        return SessionCheck(ref.session_id, "OK", detail)
    if matches(quarantine, ref, version):
        return SessionCheck(ref.session_id, "QUARANTINED", f"{quarantine['stage']}: {quarantine['error']}"[:200])  # type: ignore[index]
    return SessionCheck(ref.session_id, "MISSING", "マニフェストも隔離記録もありません（未変換）")


def _count_all(con: duckdb.DuckDBPyConnection, derived_uri: str, prefix: str, device_ids: set[str] | None) -> dict[str, int]:
    """count_rows を、指定されたデバイスごと（指定なしなら全体で 1 回）に呼んでまとめる。"""
    if not device_ids:
        return count_rows(con, derived_uri, prefix)
    merged: dict[str, int] = {}
    for device_id in sorted(device_ids):
        merged.update(count_rows(con, derived_uri, prefix, device_id))
    return merged


def run_validate(
    settings: Settings,
    fail_on_quarantine: bool = False,
    summary_file: Path | None = None,
    device_ids: set[str] | None = None,
) -> int:
    """validate コマンドの本体。戻り値は終了コード（0 = 成功）。

    device_ids を渡すと、そのデバイスのセッションと Parquet だけを検証する。
    （デバイスごとのワークフローが並行して動くとき、他のデバイスの変換途中のファイルを
    「マニフェストの無い残骸」と誤判定しないようにするため）
    """
    raw = open_location(settings.raw_uri, settings.s3)
    derived = open_location(settings.derived_uri, settings.s3)
    refs, invalid = list_raw_sessions(raw, device_ids)
    check_devices_exist(device_ids, refs, invalid)
    con = connect(settings.s3, use_s3=settings.derived_uri.startswith("s3://"))
    counts: dict[str, dict[str, int]] = {}
    unreadable: list[str] = []
    for spec in DATASETS.values():
        try:
            counts[spec.name] = _count_all(con, settings.derived_uri, spec.prefix, device_ids)
        except duckdb.Error as e:
            # Parquet として読めないファイルが混ざっている（途中で落ちた書きかけなど）
            log.error("  UNREADABLE  %s: %s", spec.prefix, str(e).splitlines()[0])
            counts[spec.name] = {}
            unreadable.append(spec.prefix)

    checks = [
        _check_session(
            ref,
            read_json(derived, manifest_key(ref.session_id)),
            read_json(derived, quarantine_key(ref.session_id)),
            settings.converter_version,
            counts,
        )
        for ref in refs
    ]
    # Parquet はあるのに、OK と判定されたセッションに含まれないもの（残骸・raw から消えたセッションなど）
    ok_ids = {c.session_id for c in checks if c.status == "OK"}
    orphans = sorted({d for per in counts.values() for d in per} - ok_ids)

    for c in checks:
        log.info("  %-11s %-32s %s", c.status, c.session_id, c.detail)
    for d in orphans:
        log.error("  ORPHAN      %-32s マニフェストの無い Parquet が残っています", d)
    for key in invalid:
        log.error("  INVALID     %s（metadata.json を読めません）", key)

    tally = Counter(c.status for c in checks)
    summary = {
        "sessions": len(checks),
        "ok": tally.get("OK", 0),
        "quarantined": tally.get("QUARANTINED", 0),
        "mismatch": tally.get("MISMATCH", 0),
        "missing": tally.get("MISSING", 0),
        "orphans": len(orphans),
        "invalid_metadata": len(invalid),
        "unreadable_datasets": len(unreadable),
    }
    log.info("検証結果: %s", json.dumps(summary, ensure_ascii=False))
    if summary_file is not None:
        summary_file.write_text(json.dumps(summary) + "\n")

    failed = summary["mismatch"] + summary["missing"] + summary["orphans"] + summary["invalid_metadata"] + summary["unreadable_datasets"] > 0
    if failed:
        log.error("検証 NG")
        return 1
    if fail_on_quarantine and summary["quarantined"] > 0:
        log.error("隔離されたセッションがあります（--fail-on-quarantine が指定されているため失敗にします）")
        return 1
    log.info("検証 OK")
    return 0
