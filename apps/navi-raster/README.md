# apps/navi-raster — 経路ラスタ（route raster）の描画

## これは何か

予定ルート（緯度経度の点列）と、そのフレームの時刻の前後の GNSS から、
**自分中心・進行方向が上** の 1 チャンネル画像（既定 128×128 px、0.5 m/px）を描くライブラリです（フェーズ 5）。
学習器の動画ローダー（[apps/trainer](../trainer/README.md) の `video_dataset.py`）が、サンプルごとにその場で描きます。

```python
from navi_raster import estimate_pose, render
pose = estimate_pose(gnss_points, t_ns, route)   # gnss_points: [(時刻 ns, 緯度, 経度), …]、route: [(緯度, 経度), …]
img = render(route, pose)                         # numpy の uint8 配列（128×128）
```

| ファイル | 役割 |
|---|---|
| `geo.py` | 緯度経度 ↔ 平面（東 x, 北 y [m]）の変換（狭い範囲なので平面近似） |
| `pose.py` | 位置と向きの推定。1 秒分の GNSS に直線を当てはめ、その傾きを向きにする。1 秒で 1m 未満しか動いていなければ、予定ルートの接線を使う（`Pose.source` = `gnss` / `route`）。IMU を使わない理由は [docs/architecture.md](../../docs/architecture.md) の「フェーズ 5」 |
| `render.py` | 描画。自分の位置は横の中央・下から 1/4（前方 48m・後方 16m・左右 32m が入る）。これから進むルートが 255、通り過ぎたルートが 96、自分の印が 160 |

依存は numpy と Pillow だけで、学習器のイメージ（Ray の公式イメージ）と同じ版にしています。

## 実行手順

ライブラリなので単独では動かしません。学習器から使います（`trainer bench-video --preview-dir` で、カメラ画像と並べた PNG を保存できる）。

## 動作確認コマンド

```bash
cd apps/navi-raster
uv sync
uv run pytest -q          # 期待: 3 passed（向きの推定、止まっているとき、描画の向きと明るさ）
```

## よくあるエラーと対処

| 症状 | 原因と対処 |
|---|---|
| `GNSS の点が 2 つ以上必要です` | そのチャンクに GNSS が無い。生成器の `--video` で作った MCAP なら 1 チャンクに 10 点ある |
| 経路ラスタが真っ黒（自分の印だけ） | 予定ルートが空か、自分の位置から 64m 以上離れている。索引の `index/routes/` に点があるか確かめる |
| 経路が左右逆に見える | 向き（heading）の定義は「東 = 0、反時計回りが正」。自前で向きを渡すときは、北 = 0 の方位角と取り違えていないか確かめる |
