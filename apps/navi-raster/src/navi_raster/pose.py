"""フレームの時刻の位置と向き（heading）を、GNSS の点から推定する。

向きの決め方（GNSS と IMU のどちらを使うか）:
- GNSS: 時刻の近い数点（このリポジトリでは同じ GOP = 1 秒分の 10 点）に直線を当てはめ、その傾きを進行方向にする。
  1 点ずつの差分は GNSS の誤差（約 0.5m）で大きくぶれるが、1 秒分をまとめて当てはめると安定する。
- IMU（角速度）を積分する方法は、積分のずれ（ドリフト）がたまり、どこから積分を始めるかの状態も要る。
  サンプルを順不同に取り出す学習のローダーでは「前の状態」を持てないので使わない。
- 止まっている（またはほぼ動いていない）とき: GNSS の点が誤差の範囲でばらつくだけで、向きが決まらない。
  1 秒の移動量が MIN_MOVE_M 未満なら、予定ルート上の一番近い点での経路の向き（接線）を使う。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from navi_raster.geo import EARTH_RADIUS_M, LocalFrame

MIN_MOVE_M = 1.0  # 当てはめた直線での移動量がこれ未満なら「止まっている」とみなす


@dataclass(frozen=True)
class Pose:
    lat: float
    lon: float
    heading_rad: float  # 東を 0、反時計回りを正とする角度（数学の向き）
    source: str  # 向きをどこから決めたか: "gnss"（当てはめ）か "route"（止まっていたので経路の向き）


def _route_tangent(frame: LocalFrame, route: Sequence[tuple[float, float]], x: float, y: float) -> float:
    rx, ry = frame.to_xy(np.array([p[0] for p in route]), np.array([p[1] for p in route]))
    i = int(np.argmin(np.hypot(rx - x, ry - y)))
    j = min(i + 1, len(route) - 1)
    i = j - 1 if j == i else i  # 最後の点なら、1 つ前の点からの向き
    return math.atan2(float(ry[j] - ry[i]), float(rx[j] - rx[i]))


def estimate_pose(gnss: Sequence[tuple[int, float, float]], t_ns: int, route: Sequence[tuple[float, float]]) -> Pose:
    """gnss: (時刻 ns, 緯度, 経度) の列（2 点以上）。t_ns: 位置と向きを知りたい時刻。"""
    if len(gnss) < 2:
        raise ValueError("GNSS の点が 2 つ以上必要です")
    frame = LocalFrame(gnss[0][1], gnss[0][2])
    t = (np.array([g[0] for g in gnss], dtype=np.float64) - t_ns) / 1e9  # 知りたい時刻を 0 とした秒
    x, y = frame.to_xy(np.array([g[1] for g in gnss]), np.array([g[2] for g in gnss]))
    # x(t) = vx * t + x0、y(t) = vy * t + y0 を最小二乗で当てはめる → t=0 の位置 (x0, y0) と速度 (vx, vy)
    vx, x0 = np.polyfit(t, x, 1)
    vy, y0 = np.polyfit(t, y, 1)
    moved = math.hypot(vx, vy) * float(t.max() - t.min())
    if moved >= MIN_MOVE_M or len(route) < 2:
        heading, source = math.atan2(vy, vx), "gnss"
    else:
        heading, source = _route_tangent(frame, route, float(x0), float(y0)), "route"
    lat = frame.lat0 + math.degrees(y0 / EARTH_RADIUS_M)
    lon = frame.lon0 + math.degrees(x0 / (EARTH_RADIUS_M * math.cos(math.radians(frame.lat0))))
    return Pose(lat, lon, heading, source)
