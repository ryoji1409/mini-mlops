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


class UnknownDeviceError(ValueError):
    """指定されたデバイスのセッションが raw に 1 件も無い（デバイス ID の打ち間違いなど）。"""


@dataclass(frozen=True)
class DiscoverResult:
    pending: list[SessionRef]  # これから変換するセッション
    states: dict[str, str]  # session_id → 状態（converted / quarantined / pending / forced）
    invalid_metadata: list[str]  # 読めなかった metadata.json（変換対象にできない）


def _raw_prefixes(device_ids: set[str] | None) -> list[str]:
    """raw を一覧するときのディレクトリ。デバイスの指定があれば、そのデバイスの下だけを見る。

    raw のキーは device=<id>/date=…/session_id=…/ の順なので、デバイスで絞るときは
    「device=<id>/」の下だけを一覧すればよい（他のデバイスの metadata.json を読まずに済む）。
    """
    if not device_ids:
        return [""]  # 指定なし = raw 全体
    return [f"device={d}" for d in sorted(device_ids)]


def list_raw_sessions(raw: Location, device_ids: set[str] | None = None) -> tuple[list[SessionRef], list[str]]:
    """raw ゾーンのセッションを列挙する。metadata.json を起点にする（最後にアップロードされる = 揃った印）。

    device_ids を渡すと、そのデバイスのセッションだけを返す（None / 空なら全デバイス）。
    """
    refs: list[SessionRef] = []
    invalid: list[str] = []
    keys = [k for prefix in _raw_prefixes(device_ids) for k in list_files(raw, prefix, suffix="/metadata.json")]
    for key in keys:
        try:
            refs.append(session_ref_from_metadata(key, read_bytes(raw, key)))
        except Exception as e:  # noqa: BLE001  1 つ壊れていても他のセッションは続ける
            log.warning("metadata.json を読めません（このセッションは対象外）: %s: %s", key, e)
            invalid.append(key)
    return sorted(refs, key=lambda r: r.session_id), invalid


def check_devices_exist(device_ids: set[str] | None, refs: list[SessionRef], invalid: list[str]) -> None:
    """指定したデバイスが raw に存在するか確かめる。1 件もセッションが無いデバイスがあれば UnknownDeviceError。

    デバイス ID を打ち間違えると、対象 0 件のまま「何もせずに成功」してしまう（フェーズ 3 の確認で実際に起きた）。
    指定したのに見つからないのは間違いの可能性が高いので、黙って成功させずにエラーにする。
    metadata.json が壊れていて読めないセッションも「そのデバイスはある」とみなす（それは別のエラーとして報告される）。
    """
    if not device_ids:
        return  # 指定なし = 全デバイス。確かめるものが無い
    found = {r.device_id for r in refs}
    # 読めなかった metadata.json のキー（device=<id>/date=…/…）からもデバイス ID を拾う
    found |= {key.split("/", 1)[0].removeprefix("device=") for key in invalid}
    missing = sorted(device_ids - found)
    if missing:
        raise UnknownDeviceError(f"raw にセッションが 1 件も無いデバイスが指定されました: {', '.join(missing)}（デバイス ID を確認してください）")


def find_pending(
    raw: Location,
    derived: Location,
    converter_version: str,
    force: bool = False,
    session_ids: set[str] | None = None,
    device_ids: set[str] | None = None,
) -> DiscoverResult:
    """変換が必要なセッションを返す。

    - force=False: マニフェストも隔離記録も「同じ入力 sha256・同じ変換器バージョン」でなければ対象
    - force=True : 状態に関係なくすべて対象（作り直し）
    - session_ids  : 指定したときは、そのセッションだけに絞る
    - device_ids   : 指定したときは、そのデバイスのセッションだけに絞る
                     （フェーズ 3: デバイスごとに別々のワークフロー / RayJob で並行に変換するため）
    """
    refs, invalid = list_raw_sessions(raw, device_ids)
    check_devices_exist(device_ids, refs, invalid)
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
