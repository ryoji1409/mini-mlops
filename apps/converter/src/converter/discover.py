"""未処理のセッションを探す（raw の metadata.json と derived の _manifests / _quarantine を突き合わせる）。

Argo の discover ステップと、RayJob の中の convert の両方から呼ぶ。
同じ判定を 2 回するが、どちらも読み取りだけで副作用が無い（冪等）ので問題ない。
こうすると Argo と Ray の間でセッションの一覧を受け渡す必要がなくなる。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from converter.layout import SessionRef, session_ref_from_metadata
from converter.records import session_state
from converter.storage import Location, list_files, read_bytes

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoverResult:
    pending: list[SessionRef]  # これから変換するセッション
    states: dict[str, str]  # session_id → 状態（converted / quarantined / pending / forced）
    invalid_metadata: list[str]  # 読めなかった metadata.json（変換対象にできない）


def list_raw_sessions(raw: Location) -> tuple[list[SessionRef], list[str]]:
    """raw ゾーンの全セッションを列挙する。metadata.json を起点にする（最後にアップロードされる = 揃った印）。"""
    refs: list[SessionRef] = []
    invalid: list[str] = []
    for key in list_files(raw, "", suffix="/metadata.json"):
        try:
            refs.append(session_ref_from_metadata(key, read_bytes(raw, key)))
        except Exception as e:  # noqa: BLE001  1 つ壊れていても他のセッションは続ける
            log.warning("metadata.json を読めません（このセッションは対象外）: %s: %s", key, e)
            invalid.append(key)
    return sorted(refs, key=lambda r: r.session_id), invalid


def find_pending(
    raw: Location,
    derived: Location,
    converter_version: str,
    force: bool = False,
    session_ids: set[str] | None = None,
) -> DiscoverResult:
    """変換が必要なセッションを返す。

    - force=False: マニフェストも隔離記録も「同じ入力 sha256・同じ変換器バージョン」でなければ対象
    - force=True : 状態に関係なくすべて対象（作り直し）
    - session_ids  : 指定したときは、そのセッションだけに絞る
    """
    refs, invalid = list_raw_sessions(raw)
    if session_ids:
        refs = [r for r in refs if r.session_id in session_ids]
    states: dict[str, str] = {}
    pending: list[SessionRef] = []
    for ref in refs:
        state = session_state(derived, ref, converter_version)
        if force:
            states[ref.session_id] = f"forced({state})"
            pending.append(ref)
        else:
            states[ref.session_id] = state
            if state == "pending":
                pending.append(ref)
    return DiscoverResult(pending=pending, states=states, invalid_metadata=invalid)


def log_summary(result: DiscoverResult) -> None:
    """判定結果を人が読める形でログに出す。"""
    for session_id, state in sorted(result.states.items()):
        log.info("  %-32s %s", session_id, state)
    counts = Counter(s.split("(")[0] for s in result.states.values())
    log.info(
        "セッション %d 件: 変換対象 %d / 変換済み %d / 隔離済み %d / metadata 不正 %d",
        len(result.states),
        len(result.pending),
        counts.get("converted", 0),
        counts.get("quarantined", 0),
        len(result.invalid_metadata),
    )
