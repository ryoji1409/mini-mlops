"""経路ラスタの描画。

画像の座標: 自分（ego）は横の中央・下から 1/4 の位置。進行方向が画像の上。
既定は 128x128 px、0.5 m/px → 前方 48m・後方 16m・左右 32m の範囲を描く。
画素の値: 0 = 何もない、255 = これから進むルート、96 = 通り過ぎたルート、160 = 自分の位置の印
（通った部分と、これから進む部分を区別できるようにしておく。学習で「どちらに曲がるか」を使えるように）
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from navi_raster.geo import LocalFrame
from navi_raster.pose import Pose

AHEAD_VALUE, BEHIND_VALUE, EGO_VALUE = 255, 96, 160


@dataclass(frozen=True)
class RasterSpec:
    size_px: int = 128
    m_per_px: float = 0.5
    ego_row_ratio: float = 0.75  # 自分の位置（上から何割の行か）
    line_width_px: int = 3


def _to_pixels(route: Sequence[tuple[float, float]], pose: Pose, spec: RasterSpec) -> np.ndarray:
    """ルートの点を、自分中心・進行方向が上の画素座標（列, 行）にする。"""
    frame = LocalFrame(pose.lat, pose.lon)
    x, y = frame.to_xy(np.array([p[0] for p in route]), np.array([p[1] for p in route]))
    # 進行方向を上（前方 = forward、右 = right）にする回転
    c, s = math.cos(pose.heading_rad), math.sin(pose.heading_rad)
    forward = x * c + y * s
    right = x * s - y * c
    col = spec.size_px / 2 + right / spec.m_per_px
    row = spec.size_px * spec.ego_row_ratio - forward / spec.m_per_px
    return np.stack([col, row], axis=1)


def render(route: Sequence[tuple[float, float]], pose: Pose, spec: RasterSpec = RasterSpec()) -> np.ndarray:
    """経路ラスタ（size_px × size_px、uint8）を返す。"""
    img = Image.new("L", (spec.size_px, spec.size_px), 0)
    draw = ImageDraw.Draw(img)
    if len(route) >= 2:
        px = _to_pixels(route, pose, spec)
        # 自分に一番近いルートの点を境に、通り過ぎた部分とこれから進む部分に分ける
        ego = np.array([spec.size_px / 2, spec.size_px * spec.ego_row_ratio])
        k = int(np.argmin(np.hypot(*(px - ego).T)))
        behind, ahead = px[: k + 1], px[k:]
        if len(behind) >= 2:
            draw.line([tuple(p) for p in behind], fill=BEHIND_VALUE, width=spec.line_width_px)
        if len(ahead) >= 2:
            draw.line([tuple(p) for p in ahead], fill=AHEAD_VALUE, width=spec.line_width_px)
    # 自分の位置: 上向きの小さな三角形
    cx, cy = spec.size_px / 2, spec.size_px * spec.ego_row_ratio
    draw.polygon([(cx, cy - 4), (cx - 3, cy + 3), (cx + 3, cy + 3)], fill=EGO_VALUE)
    return np.array(img, dtype=np.uint8)  # 書き込み可能な配列にする（torch.from_numpy が警告を出さないように）
