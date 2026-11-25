"""予定ルート（/navigation/route）を作るモジュール。

実際の運用では、これから進む予定の経路が別に与えられる。
合成データでは、デバイスが実際に動いた軌跡（GNSS のノイズを足す前の真の位置）を一定間隔で間引き、
その先にも少し延長したものを「予定ルート」とする。こうすると /gnss のコースと必ず整合する。
"""

from __future__ import annotations

import math

import numpy as np

from .sensors import EARTH_RADIUS_M, Trajectory

ROUTE_SPACING_M = 5.0  # ルートの点の間隔 [m]
ROUTE_EXTEND_M = 60.0  # 記録の終わりより先に延ばす長さ [m]（終盤でも「この先の道」が画像に描けるように）


def make_route(traj: Trajectory) -> list[tuple[float, float]]:
    """軌跡を ROUTE_SPACING_M ごとに間引いた（緯度, 経度）の列を返す。"""
    lat0 = float(traj.lat_deg[0])
    # 緯度経度 → 平面（東 x, 北 y [m]）。狭い範囲なので平面近似で十分
    x = np.radians(traj.lon_deg - traj.lon_deg[0]) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = np.radians(traj.lat_deg - traj.lat_deg[0]) * EARTH_RADIUS_M
    seg = np.hypot(np.diff(x), np.diff(y))
    dist = np.concatenate([[0.0], np.cumsum(seg)])  # 先頭からの道のり

    points: list[tuple[float, float]] = []
    for d in np.arange(0.0, dist[-1], ROUTE_SPACING_M):
        i = int(np.searchsorted(dist, d))
        points.append((float(traj.lat_deg[i]), float(traj.lon_deg[i])))

    # 最後の向きのまま、まっすぐ延長する
    yaw = float(traj.yaw_rad[-1])
    for k in range(1, int(ROUTE_EXTEND_M / ROUTE_SPACING_M) + 1):
        ex = x[-1] + k * ROUTE_SPACING_M * math.cos(yaw)
        ey = y[-1] + k * ROUTE_SPACING_M * math.sin(yaw)
        points.append(
            (
                float(traj.lat_deg[0]) + math.degrees(ey / EARTH_RADIUS_M),
                float(traj.lon_deg[0]) + math.degrees(ex / (EARTH_RADIUS_M * math.cos(math.radians(lat0)))),
            )
        )
    return points
