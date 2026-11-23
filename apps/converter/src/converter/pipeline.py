"""Ray を使った変換の全体の流れ（convert コマンドの本体）。

1. 未処理のセッションを探す（discover）
2. 事前チェックを Ray のタスクで並列に実行し、不合格のセッションは隔離する（preflight）
3. 合格したセッションを Ray Data の read_mcap でまとめて読み、セッション（= ファイル）ごとにまとめて変換する
   - 変換・書き込みに失敗したセッションは隔離し、他のセッションは続ける
   - read_mcap 自体が例外で止まった場合は、1 セッションずつ読み直して、読めないセッションだけを隔離する
   - ただし「入力は正常だが処理側の都合で失敗した」もの（メモリ不足でワーカーが落ちた、件数が合わない等）は
     隔離せず failed として報告し、convert コマンドを失敗（終了コード 1）にする。
     隔離すると次回以降スキップされてしまうが、これらはやり直せば成功するはずなので
4. 結果を集計して返す

Ray クラスタへの接続:
- RayJob の中では、KubeRay が環境変数 RAY_ADDRESS を設定するので、ray.init() で既存のクラスタにつながる
- ホスト（テスト）では、ray.init() がその場で小さなローカルクラスタを立ち上げる
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import ray
from mcap.exceptions import McapError
from ray.data.context import ShuffleStrategy

from converter.config import Settings
from converter.discover import find_pending, log_summary
from converter.layout import SessionRef
from converter.preflight import PreflightResult, TopicSchema, check_session
from converter.records import write_quarantine
from converter.session import CountMismatchError, convert_session
from converter.storage import Location, open_location

log = logging.getLogger(__name__)


@dataclass
class ConvertReport:
    """convert コマンドの結果（最後に JSON でログに出す）。"""

    skipped: dict[str, str] = field(default_factory=dict)  # session_id → 状態（converted / quarantined）
    converted: list[str] = field(default_factory=list)
    quarantined: dict[str, str] = field(default_factory=dict)  # session_id → 失敗した段階とエラー（入力の問題）
    failed: dict[str, str] = field(default_factory=dict)  # session_id → エラー（処理側の問題。次回やり直す）


@dataclass(frozen=True)
class _ConvertContext:
    """map_groups の各タスクに渡す情報（pickle されて Ray のワーカーに送られる）。"""

    settings: Settings
    raw: Location
    derived: Location
    sessions: dict[str, tuple[SessionRef, dict[str, TopicSchema]]]  # mcap_key → (セッション, スキーマ)

    def lookup(self, path: str) -> tuple[SessionRef, dict[str, TopicSchema]]:
        """read_mcap の path 列（ファイルのパス）からセッションを引く。

        Ray がパスの書き方を正規化する（先頭の s3:// を外すなど）ことがあるので、末尾一致で探す。
        """
        for mcap_key, value in self.sessions.items():
            if path.endswith(mcap_key):
                return value
        raise KeyError(f"対応するセッションが見つかりません: {path}")


_RESULT_SCHEMA = pa.schema([("session_id", pa.string()), ("status", pa.string()), ("error", pa.string())])


def _convert_group(group: pa.Table, ctx: _ConvertContext) -> pa.Table:
    """map_groups から呼ばれる関数。group = 1 セッション分（1 ファイル分）の全メッセージ。

    例外を外に出すとデータセット全体が止まるので、ここで捕まえて隔離し、結果の行として返す。
    """
    ref, schemas = ctx.lookup(group.column("path")[0].as_py())
    try:
        convert_session(ctx.settings, ctx.raw, ctx.derived, ref, schemas, group)
        status, error = "converted", None
    except CountMismatchError as e:
        log.error("%s: %s（隔離せず、失敗として報告します）", ref.session_id, e)
        status, error = "failed", f"convert: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("%s: 変換に失敗しました（隔離します）", ref.session_id)
        write_quarantine(ctx.settings, ctx.raw, ctx.derived, ref, "convert", e)
        status, error = "quarantined", f"convert: {type(e).__name__}: {e}"
    return pa.table({"session_id": [ref.session_id], "status": [status], "error": [error]}, schema=_RESULT_SCHEMA)


def _run_dataset(ctx: _ConvertContext, refs: list[SessionRef]) -> list[dict[str, Any]]:
    """read_mcap → セッションごとに変換、を実行して結果の行を返す。"""
    paths = [ctx.raw.path(r.mcap_key) for r in refs]
    ds = ray.data.read_mcap(
        paths,
        filesystem=ctx.raw.fs,
        include_paths=True,  # どのファイル（= どのセッション）の行かを path 列で知るため
        include_metadata=False,  # スキーマは事前チェックで取得済み。全行に付くと重いので付けない
    )
    # path ごとにまとめる = 1 セッション分のメッセージが 1 回の関数呼び出しに集まる。
    # （1 セッションが数 GB になると、1 回の呼び出しに載りきらないので、セッション内を分けて書く方式に変える必要がある）
    # 分割数をセッション数に合わせる（既定の 200 分割だと、小さなデータでは空のタスクばかりになる）
    results = ds.groupby("path", num_partitions=len(refs)).map_groups(_convert_group, batch_format="pyarrow", fn_kwargs={"ctx": ctx})
    return results.take_all()


@ray.remote
def _preflight_task(raw: Location, ref: SessionRef) -> PreflightResult:
    """事前チェックを Ray のワーカーで実行するためのラッパー。"""
    return check_session(raw, ref)


def _preflight_all(settings: Settings, raw: Location, derived: Location, refs: list[SessionRef], report: ConvertReport) -> dict[str, PreflightResult]:
    """全セッションの事前チェックを並列に実行する。不合格は隔離して、合格したセッションだけ返す。"""
    results: list[PreflightResult] = ray.get([_preflight_task.remote(raw, r) for r in refs])
    passed: dict[str, PreflightResult] = {}
    for ref, res in zip(refs, results, strict=True):
        if res.ok:
            passed[ref.session_id] = res
        else:
            log.warning("%s: 事前チェック NG（隔離します）: %s", ref.session_id, res.error)
            write_quarantine(settings, raw, derived, ref, "preflight", RuntimeError(res.error))
            report.quarantined[ref.session_id] = f"preflight: {res.error}"
    return passed


# 例外の中身にこれらが現れたら「MCAP の中身が壊れている」（= 入力の問題）とみなす
_DATA_ERROR_RE = re.compile(r"McapError|InvalidMagic|EndOfFile|RecordLengthLimitExceeded|ZstdError|CRC")


def _is_data_error(e: BaseException) -> bool:
    """例外が「入力データの破損」によるものか。

    Ray は元の例外を RayTaskError などで包むので、原因（cause / __cause__ / __context__）をたどり、
    最後に文字列（元のトレースバックを含む）でも確認する。
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [e]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, McapError):
            return True
        stack += [getattr(cur, "cause", None), cur.__cause__, cur.__context__]
    return bool(_DATA_ERROR_RE.search(str(e)))


def _retry_one_by_one(ctx: _ConvertContext, refs: list[SessionRef]) -> list[dict[str, Any]]:
    """1 セッションずつ読み直す。MCAP の破損で読めないセッションは隔離、それ以外の失敗は failed にする。"""
    rows: list[dict[str, Any]] = []
    for ref in refs:
        try:
            rows.extend(_run_dataset(ctx, [ref]))
        except Exception as e:  # noqa: BLE001
            msg = f"read: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
            if _is_data_error(e):
                log.exception("%s: MCAP が壊れていて読めません（隔離します）", ref.session_id)
                write_quarantine(ctx.settings, ctx.raw, ctx.derived, ref, "read", e)
                rows.append({"session_id": ref.session_id, "status": "quarantined", "error": msg})
            else:
                log.exception("%s: 読み込みに失敗しました（入力の破損ではないため隔離しません）", ref.session_id)
                rows.append({"session_id": ref.session_id, "status": "failed", "error": msg})
    return rows


def _convert_all(ctx: _ConvertContext, refs: list[SessionRef], report: ConvertReport) -> None:
    """まとめて変換する。read_mcap ごと失敗したら、1 セッションずつやり直して原因のセッションを特定する。"""
    try:
        rows = _run_dataset(ctx, refs)
    except Exception:  # noqa: BLE001
        log.exception("まとめて読む処理が失敗しました。1 セッションずつ読み直します")
        rows = _retry_one_by_one(ctx, refs)
    _collect(ctx, refs, rows, report)


def _collect(ctx: _ConvertContext, refs: list[SessionRef], rows: list[dict[str, Any]], report: ConvertReport) -> None:
    """結果の行を集計する。結果が返ってこなかったセッション（メッセージ 0 件など）も隔離する。"""
    seen = set()
    for row in rows:
        seen.add(row["session_id"])
        if row["status"] == "converted":
            report.converted.append(row["session_id"])
        elif row["status"] == "failed":
            report.failed[row["session_id"]] = row["error"]
        else:
            report.quarantined[row["session_id"]] = row["error"]
    for ref in refs:
        if ref.session_id not in seen:
            err = RuntimeError("read_mcap がメッセージを 1 件も返しませんでした")
            write_quarantine(ctx.settings, ctx.raw, ctx.derived, ref, "read", err)
            report.quarantined[ref.session_id] = f"read: {err}"


def run_convert(
    settings: Settings,
    force: bool = False,
    session_ids: set[str] | None = None,
    device_ids: set[str] | None = None,
) -> ConvertReport:
    """convert コマンドの本体。device_ids を渡すと、そのデバイスのセッションだけを変換する。"""
    raw = open_location(settings.raw_uri, settings.s3)
    derived = open_location(settings.derived_uri, settings.s3)
    found = find_pending(raw, derived, settings.converter_version, force, session_ids, device_ids)
    log_summary(found)
    report = ConvertReport(skipped={d: s for d, s in found.states.items() if s in ("converted", "quarantined")})
    if not found.pending:
        log.info("変換が必要なセッションはありません（すべてスキップ）")
        return report

    ray.init(ignore_reinit_error=True, log_to_driver=True)
    data_ctx = ray.data.DataContext.get_current()
    # Pod のログ（TTY ではない）に進捗バーの制御文字が大量に出ないようにする
    data_ctx.enable_progress_bars = sys.stderr.isatty()
    # groupby の並べ替え方式。既定のハッシュ方式は集約用のプロセス（actor）を何個も常駐させるため、
    # メモリの小さいワーカー（1.75Gi）ではメモリ不足でワーカーが落ちた。
    # ソート方式は普通のタスクだけで動き、使うメモリがデータ量に比例するので、こちらにする
    data_ctx.shuffle_strategy = ShuffleStrategy.SORT_SHUFFLE_PULL_BASED

    passed = _preflight_all(settings, raw, derived, found.pending, report)
    refs = [r for r in found.pending if r.session_id in passed]
    if refs:
        ctx = _ConvertContext(settings, raw, derived, {r.mcap_key: (r, passed[r.session_id].schemas) for r in refs})
        _convert_all(ctx, refs, report)
    return report
