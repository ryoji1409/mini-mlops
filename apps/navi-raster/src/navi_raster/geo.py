"""緯度経度 ↔ 平面（東 x, 北 y [m]）の変換。

数百 m の範囲しか扱わないので、基準点のまわりの平面近似（正距円筒図法）で十分（誤差はおよそ 0.1% 未満）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_378_137.0  # WGS84 の赤道半径（生成器と同じ値）


@dataclass(frozen=True)
class LocalFrame:
    """基準点（lat0, lon0）を原点とする平面座標。"""

    lat0: float
    lon0: float

    def to_xy(self, lat: np.ndarray | float, lon: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
        x = np.radians(np.asarray(lon) - self.lon0) * EARTH_RADIUS_M * math.cos(math.radians(self.lat0))
        y = np.radians(np.asarray(lat) - self.lat0) * EARTH_RADIUS_M
        return x, y
