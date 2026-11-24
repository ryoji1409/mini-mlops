"""コマンドラインの入口。

    converter discover [--force] [--session-id ID ...] [--device-id ID ...] [--output-file PATH]
        未処理のセッションを数える（Argo の discover ステップ）。件数を --output-file に書く
    converter convert  [--force] [--session-id ID ...] [--device-id ID ...]
        Ray で変換する（RayJob の entrypoint）
    converter validate [--fail-on-quarantine] [--device-id ID ...] [--summary-file PATH]
        DuckDB で行数を検証する（Argo の validate ステップ）

--force は Argo のパラメータをそのまま渡せるように、--force / --force=true / --force=false のどれでも受け付ける。
--device-id も同じ理由で、空の値（--device-id=）は「指定なし = 全デバイス」として扱う。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from converter.config import load_settings


def _bool(value: str) -> bool:
    """"true" / "false" などの文字列を bool にする。"""
    v = value.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"true / false を指定してください: {value}")


def _add_device_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--device-id", action="append", default=None, help="対象のデバイスを絞る（複数指定可。空なら全デバイス）")


def _device_ids(args: argparse.Namespace) -> set[str]:
    """--device-id の値を集合にする。Argo のパラメータが空のとき（--device-id=）は除く → 空集合 = 全デバイス。"""
    return {d.strip() for d in (args.device_id or []) if d.strip()}


def _add_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--force", type=_bool, nargs="?", const=True, default=False, help="変換済み・隔離済みのセッションも作り直す")
    p.add_argument("--session-id", action="append", default=None, help="対象のセッションを絞る（複数指定可）")
    _add_device_arg(p)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="converter", description="MCAP → Parquet 変換器")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="未処理のセッションを数える")
    _add_target_args(p)
    p.add_argument("--output-file", type=Path, help="変換対象の件数を書き出すファイル（Argo の出力パラメータ用）")

    p = sub.add_parser("convert", help="Ray で変換する")
    _add_target_args(p)

    p = sub.add_parser("validate", help="DuckDB で行数を検証する")
    p.add_argument("--fail-on-quarantine", type=_bool, nargs="?", const=True, default=False, help="隔離されたセッションがあれば失敗にする")
    p.add_argument("--summary-file", type=Path, help="検証結果の集計（JSON）を書き出すファイル")
    _add_device_arg(p)
    return parser


def _cmd_discover(args: argparse.Namespace) -> int:
    from converter.discover import find_pending, log_summary
    from converter.storage import open_location

    settings = load_settings()
    raw = open_location(settings.raw_uri, settings.s3)
    derived = open_location(settings.derived_uri, settings.s3)
    result = find_pending(raw, derived, settings.converter_version, args.force, set(args.session_id or []), _device_ids(args))
    log_summary(result)
    if args.output_file:
        args.output_file.write_text(f"{len(result.pending)}\n")
    return 0


def _cmd_convert(args: argparse.Namespace) -> int:
    # ray は import に時間がかかるので、convert のときだけ読み込む
    from converter.pipeline import run_convert

    report = run_convert(load_settings(), args.force, set(args.session_id or []), _device_ids(args))
    logging.getLogger("converter").info("変換結果: %s", json.dumps(asdict(report), ensure_ascii=False))
    # 隔離されたセッションがあっても、変換処理そのものは成功（0）とする。判定は validate ステップで行う。
    # 処理側の問題で失敗したセッション（failed）があれば 1 を返し、RayJob → ワークフローを失敗にする
    return 1 if report.failed else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from converter.validate import run_validate

    return run_validate(load_settings(), args.fail_on_quarantine, args.summary_file, _device_ids(args))


def main(argv: list[str] | None = None) -> int:
    from converter.discover import UnknownDeviceError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = _parser().parse_args(argv)
    handler = {"discover": _cmd_discover, "convert": _cmd_convert, "validate": _cmd_validate}[args.command]
    try:
        return handler(args)
    except UnknownDeviceError as e:
        # 存在しないデバイスの指定は、スタックトレースではなく 1 行のエラーにして失敗（終了コード 1）にする。
        # Argo の discover ステップが失敗するので、ワークフローも失敗になり、間違いに気づける
        logging.getLogger("converter").error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
