"""経路ラスタ（route raster）を描くライブラリ。

学習のサンプルごとに、その場で描く（事前に画像として保存しない）。
- 入力: 予定ルート（緯度経度の点列）と、そのフレームの時刻の前後の GNSS の位置
- 出力: 自分を中心より少し下に置き、進行方向を上にした 1 チャンネルの画像（numpy の uint8 配列）

    pose = estimate_pose(gnss_points, t_ns, route)
    img = render(route, pose)

主な関数は pose.py（位置と向きの推定）と render.py（描画）にある。
"""

from navi_raster.geo import LocalFrame
from navi_raster.pose import Pose, estimate_pose
from navi_raster.render import RasterSpec, render

__all__ = ["LocalFrame", "Pose", "RasterSpec", "estimate_pose", "render"]
