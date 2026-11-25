from __future__ import annotations

import math

import numpy as np

from navi_raster import RasterSpec, estimate_pose, render
from navi_raster.geo import EARTH_RADIUS_M
from navi_raster.render import AHEAD_VALUE, BEHIND_VALUE

LAT0, LON0 = 35.0, 139.0


def _latlon(x: float, y: float) -> tuple[float, float]:
    """基準点から東 x m・北 y m の緯度経度。"""
    return LAT0 + math.degrees(y / EARTH_RADIUS_M), LON0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(LAT0))))


def _gnss_along(heading_deg: float, speed: float, noise: float = 0.0, seed: int = 0) -> list[tuple[int, float, float]]:
    rng = np.random.default_rng(seed)
    h = math.radians(heading_deg)
    pts = []
    for k in range(10):  # 1 秒分、10Hz
        d = speed * k * 0.1
        pts.append((k * 100_000_000, *_latlon(d * math.cos(h) + rng.normal(0, noise), d * math.sin(h) + rng.normal(0, noise))))
    return pts


def test_GNSSの当てはめで向きが分かる() -> None:
    for deg in (0, 90, 135, -60):
        pose = estimate_pose(_gnss_along(deg, 8.0, noise=0.5, seed=deg % 7), 500_000_000, route=[])
        err = (math.degrees(pose.heading_rad) - deg + 180) % 360 - 180
        assert pose.source == "gnss" and abs(err) < 10, (deg, err)


def test_止まっているときは経路の向きを使う() -> None:
    route = [_latlon(0, 5.0 * k) for k in range(-5, 20)]  # 真北に向かう経路
    pose = estimate_pose(_gnss_along(30, 0.0, noise=0.3, seed=1), 500_000_000, route)
    assert pose.source == "route" and abs(math.degrees(pose.heading_rad) - 90) < 1


def test_進行方向が上で_前方の経路が明るい() -> None:
    # 東に進み、自分の 20m 先で左（北）に曲がる経路
    route = [_latlon(x, 0) for x in range(-20, 21, 5)] + [_latlon(20, y) for y in range(5, 41, 5)]
    pose = estimate_pose(_gnss_along(0, 8.0), 0, route)
    img = render(route, pose, RasterSpec())
    assert img.shape == (128, 128) and img.dtype == np.uint8
    # 自分（列 64、行 96）の真上（前方 10m = 20px 上）はこれから進む経路
    assert img[96 - 20, 64] == AHEAD_VALUE
    # 後方 10m（20px 下）は通り過ぎた経路
    assert img[96 + 20, 64] == BEHIND_VALUE
    # 前方 20m で左に曲がる → 曲がった先（前方 20m・左 10m）が明るい。右側は暗い
    assert img[96 - 40, 64 - 20] == AHEAD_VALUE and img[96 - 40, 64 + 20] == 0
