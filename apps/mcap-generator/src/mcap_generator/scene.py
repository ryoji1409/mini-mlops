"""カメラの画像（簡易な抽象シーン）を Pillow で描くモジュール。

シーンの構成: 空と、格子模様の平らな床。床のあちこちに低い標識が立ち、
前方に大きな物体（立方体）、左右に小さな物体（円柱）が置かれている。カメラは床の上をまっすぐ前へ進む。

本物らしさより「条件の違いが画像にはっきり出ること」を優先している。
後のフェーズで画像埋め込み（SigLIP）を使って「夜」「雨」などで検索したとき、
該当フレームが上位に来るように、時間帯・天候で色や見た目を大きく変える。

座標の考え方（簡易的な透視投影）:
  カメラは床から高さ CAMERA_HEIGHT_M、前方を向いている。
  前方 d メートル・横 lat メートルの床上の点は、画面上で
    y = HORIZON_Y + FOCAL_PX * CAMERA_HEIGHT_M / d
    x = CENTER_X  + FOCAL_PX * lat / d
  に写る（遠いほど地平線に近づき、小さくなる）。
"""

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .scenario import SessionScenario

WIDTH, HEIGHT = 640, 360
HORIZON_Y = 150
CENTER_X = WIDTH // 2
FOCAL_PX = 700.0
CAMERA_HEIGHT_M = 1.5

GRID_M = 2.0  # 床の格子の間隔 [m]
SIDE_LAT_M = 4.5  # 小さな物体を置く横位置（進路の外側）
MARKER_LATS_M: tuple[float, ...] = (5.0, 9.0, 7.0, 12.0)  # 標識の横位置（順番に使い、列に見えないようにする）
NEAR_DIST_M = FOCAL_PX * CAMERA_HEIGHT_M / (HEIGHT - HORIZON_Y)  # 画面下端に写る距離（=5m）
FAR_DIST_M = 200.0

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class Palette:
    """時間帯ごとの色の組み合わせ。"""

    sky_top: RGB
    sky_bottom: RGB
    floor: RGB
    grid: RGB
    brightness: float  # 物体の色に掛ける明るさ（夜は暗くする）


PALETTES: dict[str, Palette] = {
    "day": Palette((110, 170, 235), (200, 225, 245), (165, 165, 158), (110, 110, 105), 1.0),
    "dusk": Palette((70, 60, 120), (250, 150, 80), (125, 105, 92), (80, 70, 65), 0.7),
    "night": Palette((5, 7, 20), (25, 30, 55), (28, 30, 35), (55, 58, 66), 0.25),
}

BOX_COLORS: tuple[RGB, ...] = ((200, 30, 30), (30, 60, 180), (230, 230, 230), (40, 40, 40), (200, 170, 40))
PILLAR_COLORS: tuple[RGB, ...] = ((200, 60, 60), (60, 120, 200), (240, 200, 60), (80, 80, 80), (60, 160, 90))


@dataclass(frozen=True)
class BoxObject:
    """カメラの前方で前後に揺れながら一緒に進む大きな物体（立方体）。"""

    lat_m: float  # 横位置
    base_dist_m: float  # カメラからの基準距離
    phase: float  # 距離の揺れの位相（物体ごとに動きをずらす）
    color: RGB


@dataclass(frozen=True)
class PillarObject:
    """進路の脇に止まっている小さな物体（円柱）。カメラが進むと近づいてくる。"""

    side: int  # -1 = 左, +1 = 右
    start_dist_m: float  # 記録開始時の前方距離
    color: RGB


@dataclass(frozen=True)
class SceneLayout:
    """セッション中ずっと変わらないシーンの構成（どこに物体があるか）。"""

    boxes: tuple[BoxObject, ...]
    pillars: tuple[PillarObject, ...]
    marker_spacing_m: float


def make_layout(scenario: SessionScenario, rng: np.random.Generator) -> SceneLayout:
    """セッションの条件から、物体の配置を乱数で決める。"""
    boxes = tuple(
        BoxObject(
            lat_m=float(rng.choice([-2.5, 0.0, 2.5])),
            base_dist_m=float(rng.uniform(12.0, 60.0)),
            phase=float(rng.uniform(0, 2 * math.pi)),
            color=BOX_COLORS[int(rng.integers(len(BOX_COLORS)))],
        )
        for _ in range(scenario.num_large_objects)
    )
    pillars = tuple(
        PillarObject(
            side=int(rng.choice([-1, 1])),
            start_dist_m=float(rng.uniform(15.0, 80.0)),
            color=PILLAR_COLORS[int(rng.integers(len(PILLAR_COLORS)))],
        )
        for _ in range(scenario.num_small_objects)
    )
    return SceneLayout(boxes, pillars, marker_spacing_m=9.0)


# ---------------------------------------------------------------------------
# 座標変換の小さなヘルパー
# ---------------------------------------------------------------------------


def _project(dist_m: float, lat_m: float, height_m: float = 0.0) -> tuple[float, float]:
    """前方距離・横位置・高さ（床からの）を画面座標 (x, y) に変換する。"""
    x = CENTER_X + FOCAL_PX * lat_m / dist_m
    y = HORIZON_Y + FOCAL_PX * (CAMERA_HEIGHT_M - height_m) / dist_m
    return x, y


def _scale(color: RGB, k: float) -> RGB:
    """色の明るさを k 倍する（0〜255 に収める）。"""
    return tuple(max(0, min(255, int(c * k))) for c in color)  # type: ignore[return-value]


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    """2 色の間を t（0〜1）で補間する。グラデーションに使う。"""
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 各要素の描画
# ---------------------------------------------------------------------------


def _draw_sky_and_floor(draw: ImageDraw.ImageDraw, pal: Palette, time_of_day: str) -> None:
    for y in range(HORIZON_Y):
        draw.line([(0, y), (WIDTH, y)], fill=_lerp(pal.sky_top, pal.sky_bottom, y / HORIZON_Y))
    if time_of_day == "dusk":
        # 夕日: 地平線に半分沈みかけたオレンジの太陽（床を後から塗って下半分を隠す）
        draw.ellipse([470, HORIZON_Y - 40, 530, HORIZON_Y + 20], fill=(255, 190, 90))
    elif time_of_day == "day":
        draw.ellipse([80, 20, 120, 60], fill=(255, 250, 220))
    draw.rectangle([0, HORIZON_Y, WIDTH, HEIGHT], fill=pal.floor)


def _draw_floor_grid(draw: ImageDraw.ImageDraw, pal: Palette, travelled_m: float) -> None:
    """床の格子。横線を進んだ距離ぶんずらすと、前に進んでいるように見える。"""
    # 縦線（進行方向に平行）
    for k in range(-12, 13):
        lat = k * GRID_M
        draw.line([_project(NEAR_DIST_M, lat), _project(FAR_DIST_M, lat)], fill=pal.grid, width=1)
    # 横線（進行方向に垂直）
    d = NEAR_DIST_M + GRID_M - (travelled_m % GRID_M)
    while d < 80.0:
        y = _project(d, 0.0)[1]
        draw.line([(0, y), (WIDTH, y)], fill=pal.grid, width=1 if d > 15.0 else 2)
        d += GRID_M


def _draw_markers(draw: ImageDraw.ImageDraw, layout: SceneLayout, travelled_m: float, time_of_day: str) -> None:
    """床に立つ低い標識。夜と夕方は先端が光る（夜の画像の特徴になる）。

    標識は床に固定されているので、k 番目の標識の位置は「k × 間隔」。
    左右交互に置き、横位置も k ごとに変えて、整った列に見えないようにする。
    """
    lit = time_of_day in ("night", "dusk")
    spacing = layout.marker_spacing_m
    k = int(travelled_m // spacing) + 1  # まだ通り過ぎていない最初の標識の番号
    markers: list[tuple[float, float]] = []
    while (d := k * spacing - travelled_m) < 150.0:
        if d > NEAR_DIST_M:
            side = 1 if k % 2 else -1
            markers.append((d, side * MARKER_LATS_M[k % len(MARKER_LATS_M)]))
        k += 1
    for d, lat in sorted(markers, reverse=True):  # 遠い順に描く（近いものが手前に重なる）
        base = _project(d, lat, 0.0)
        top = _project(d, lat, 1.0)
        draw.line([base, top], fill=(70, 70, 75), width=max(1, int(30 / d)))
        if lit:
            r = max(2.0, 80 / d)
            glow = r * 3
            draw.ellipse([top[0] - glow, top[1] - glow, top[0] + glow, top[1] + glow], fill=(140, 220, 255, 60))
            draw.ellipse([top[0] - r, top[1] - r, top[0] + r, top[1] + r], fill=(210, 245, 255))


def _draw_spotlight(draw: ImageDraw.ImageDraw) -> None:
    """デバイスの照明で、手前の床が丸く明るく照らされている様子（遠近で横長の楕円に見える）。"""
    for k, alpha in ((1.0, 35), (0.6, 45)):  # 外側は薄く、内側は濃く重ねて、ぼんやりした光にする
        x0, _ = _project(9.0, -3.5 * k)
        x1, _ = _project(9.0, 3.5 * k)
        _, y0 = _project(9.0 + 6.0 * k, 0.0)
        _, y1 = _project(max(NEAR_DIST_M, 9.0 - 3.0 * k), 0.0)
        draw.ellipse([x0, y0, x1, y1], fill=(255, 245, 200, alpha))


def _box_dist(box: BoxObject, t_s: float) -> float:
    """箱の前方距離（ゆっくり前後に揺れる）。"""
    return box.base_dist_m + 4.0 * math.sin(0.3 * t_s + box.phase)


def _draw_box(draw: ImageDraw.ImageDraw, box: BoxObject, t_s: float, pal: Palette, time_of_day: str) -> None:
    dist = _box_dist(box, t_s)
    half, size = 0.6, 1.2  # 1 辺 1.2m の立方体
    x0, y_bottom = _project(dist, box.lat_m - half, 0.0)
    x1, y_top = _project(dist, box.lat_m + half, size)
    # 上面（奥に向かって少しだけ見える）→ 正面の順に描く
    _, y_top_far = _project(dist + size, box.lat_m, size)
    xf0, _ = _project(dist + size, box.lat_m - half, size)
    xf1, _ = _project(dist + size, box.lat_m + half, size)
    draw.polygon([(x0, y_top), (x1, y_top), (xf1, y_top_far), (xf0, y_top_far)], fill=_scale(box.color, pal.brightness * 1.3))
    draw.rectangle([x0, y_top, x1, y_bottom], fill=_scale(box.color, pal.brightness))
    # 上に載った表示灯 1 つ（夜・夕方は明るく光る）
    lamp_r = max(1.5, (x1 - x0) * 0.08)
    cx, cy = (x0 + x1) / 2, y_top_far - lamp_r
    if time_of_day in ("night", "dusk"):
        g = lamp_r * 4
        draw.ellipse([cx - g, cy - g, cx + g, cy + g], fill=(120, 255, 160, 70))
        draw.ellipse([cx - lamp_r, cy - lamp_r, cx + lamp_r, cy + lamp_r], fill=(170, 255, 200))
    else:
        draw.ellipse([cx - lamp_r, cy - lamp_r, cx + lamp_r, cy + lamp_r], fill=(60, 150, 90))


def _pillar_dist(pillar: PillarObject, travelled_m: float) -> float:
    # 柱は止まっているので、カメラが進むと相対的に近づいてくる。
    # 通り過ぎたら（近すぎたら）また遠くに出てくるように距離をループさせる。
    loop_len = 90.0
    return NEAR_DIST_M + 3.0 + ((pillar.start_dist_m - travelled_m) % loop_len)


def _draw_pillar(draw: ImageDraw.ImageDraw, pillar: PillarObject, travelled_m: float, pal: Palette) -> None:
    dist = _pillar_dist(pillar, travelled_m)
    lat = pillar.side * SIDE_LAT_M
    (xf, yf), (_, yh) = _project(dist, lat, 0.0), _project(dist, lat, 1.2)
    body_w = max(2.0, FOCAL_PX * 0.4 / dist)
    cap_r = body_w * 0.6
    color = _scale(pillar.color, max(pal.brightness, 0.35))
    draw.rectangle([xf - body_w / 2, yh + cap_r, xf + body_w / 2, yf], fill=color)
    cap = _scale((235, 235, 235), max(pal.brightness, 0.35))
    draw.ellipse([xf - cap_r, yh, xf + cap_r, yh + cap_r * 2], fill=cap)


def _apply_rain(img: Image.Image, pal: Palette, rng: np.random.Generator) -> Image.Image:
    """雨: 全体を灰色がかった低コントラストにし、雨筋を描いて少しぼかす。

    混ぜる灰色は時間帯の明るさに合わせる（夜に明るい灰色を混ぜると「夜らしさ」が消えるため）。
    """
    brightness = max(pal.brightness, 0.3)
    gray = Image.new("RGB", img.size, _scale((120, 125, 135), brightness))
    img = Image.blend(img, gray, 0.35)
    draw = ImageDraw.Draw(img, "RGBA")
    xs = rng.uniform(0, WIDTH, 220)
    ys = rng.uniform(-20, HEIGHT, 220)
    lengths = rng.uniform(8, 22, 220)
    streak = (*_scale((210, 215, 225), brightness + 0.2), 140)
    for x, y, length in zip(xs, ys, lengths, strict=True):
        draw.line([(x, y), (x - length * 0.25, y + length)], fill=streak, width=1)
    return img.filter(ImageFilter.GaussianBlur(0.8))


# ---------------------------------------------------------------------------
# 外から呼ぶ関数
# ---------------------------------------------------------------------------


def render_frame(
    scenario: SessionScenario,
    layout: SceneLayout,
    t_s: float,
    travelled_m: float,
    rng: np.random.Generator,
) -> Image.Image:
    """記録開始から t_s 秒後、移動距離 travelled_m のときのフレームを 1 枚描く。"""
    pal = PALETTES[scenario.time_of_day]
    # "RGBA" を指定すると、半透明の色（光のにじみなど）を重ねて描ける
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img, "RGBA")

    _draw_sky_and_floor(draw, pal, scenario.time_of_day)
    _draw_floor_grid(draw, pal, travelled_m)
    if scenario.time_of_day == "night":
        _draw_spotlight(draw)
    _draw_markers(draw, layout, travelled_m, scenario.time_of_day)

    # 遠いものから順に描く（手前のものが上に重なるように）
    for pillar in sorted(layout.pillars, key=lambda p: -_pillar_dist(p, travelled_m)):
        _draw_pillar(draw, pillar, travelled_m, pal)
    for box in sorted(layout.boxes, key=lambda b: -_box_dist(b, t_s)):
        _draw_box(draw, box, t_s, pal, scenario.time_of_day)

    if scenario.weather == "rain":
        img = _apply_rain(img, pal, rng)
    return img
