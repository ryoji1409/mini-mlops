"""コマンドラインの入口。

使い方の例:
    uv run mcap-generate --devices 2 --sessions-per-device 3 --out ./output --seed 42
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from .scenario import make_scenarios
from .writer import write_session


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mcap-generate",
        description="合成 MCAP（カメラ / IMU / GNSS）と metadata.json をセッションごとに生成する",
    )
    p.add_argument("--devices", type=int, default=2, help="デバイス数（既定: 2）")
    p.add_argument("--sessions-per-device", type=int, default=3, help="デバイス 1 台あたりのセッション数（既定: 3）")
    p.add_argument("--out", type=Path, default=Path("output"), help="出力先ディレクトリ（既定: ./output）")
    p.add_argument("--seed", type=int, default=42, help="乱数シード。同じ値なら同じファイルができる（既定: 42）")
    p.add_argument("--min-duration", type=int, default=30, help="記録時間の最小 [秒]（既定: 30）")
    p.add_argument("--max-duration", type=int, default=60, help="記録時間の最大 [秒]（既定: 60）")
    p.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date(2026, 11, 1),
        help="記録日の起点（YYYY-MM-DD）。ここから 14 日の範囲に記録日をばらまく（既定: 2026-11-01）",
    )
    args = p.parse_args(argv)
    if args.devices < 1 or args.sessions_per_device < 1:
        p.error("--devices と --sessions-per-device は 1 以上を指定してください")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    scenarios = make_scenarios(
        num_devices=args.devices,
        sessions_per_device=args.sessions_per_device,
        seed=args.seed,
        min_duration_s=args.min_duration,
        max_duration_s=args.max_duration,
        start_date=args.start_date,
    )
    print(f"{len(scenarios)} セッション分の MCAP を {args.out} に生成します（seed={args.seed}）")

    for n, scenario in enumerate(scenarios, start=1):
        started = time.monotonic()
        meta = write_session(scenario, args.out, args.seed)
        counts = ", ".join(f"{t['topic']}={t['message_count']}" for t in meta["topics"])
        print(
            f"[{n}/{len(scenarios)}] {scenario.session_id} "
            f"{scenario.time_of_day}/{scenario.weather} "
            f"大{scenario.num_large_objects} 小{scenario.num_small_objects} "
            f"{scenario.duration_s}s {meta['file']['size_bytes'] / 1e6:.1f}MB "
            f"({counts}) {time.monotonic() - started:.1f}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
