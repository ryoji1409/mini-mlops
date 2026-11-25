"""カメラ映像を H.265 で圧縮するモジュール（--video のときだけ使う）。

設計（後段の「GOP 単位で取り出してデコード」を成り立たせるための決まり）:
- 10Hz、GOP 長 1 秒（10 フレーム）で固定。GOP の先頭は必ず IDR フレーム（それだけで復元できるフレーム）
- B フレームなし（後ろのフレームを参照しない → 先頭から順に読めばデコードできる）
- 各 IDR に VPS / SPS / PPS（復号に必要な設定）を付ける（x265 の repeat-headers=1）。
  途中の GOP だけを取り出しても、その GOP の先頭に設定が付いているのでデコードできる
- NAL（H.265 のデータの単位）は Annex B 形式（00 00 00 01 の開始コードで区切る）。1 メッセージ = 1 フレーム
- 低遅延（tune=zerolatency）にして、フレームを入れたらすぐ圧縮データが出てくるようにする
  （先読みで遅れると、MCAP に時刻順で書くのが難しくなるため）
"""

from __future__ import annotations

from fractions import Fraction

import av
import numpy as np
from PIL import Image

VIDEO_RATE_HZ = 10
GOP_LENGTH = 10  # 1 GOP = 10 フレーム = 1 秒
VIDEO_WIDTH, VIDEO_HEIGHT = 320, 180  # 元の描画（640x360）を半分に縮小する

# x265 の設定。scenecut=0 は「場面転換を自動検出して IDR を足す」を止め、GOP 長を必ず 10 にする
X265_PARAMS = ":".join(
    [
        f"keyint={GOP_LENGTH}",
        f"min-keyint={GOP_LENGTH}",
        "bframes=0",
        "scenecut=0",
        "open-gop=0",
        "repeat-headers=1",
        "log-level=error",
    ]
)


def nal_unit_types(data: bytes) -> list[int]:
    """Annex B のバイト列に含まれる NAL の種類（H.265 の nal_unit_type）を順に返す。

    32 = VPS、33 = SPS、34 = PPS、19 / 20 = IDR、1 = 普通のフレーム（P スライス）など。
    テストと確認用。
    """
    types, i = [], 0
    while (j := data.find(b"\x00\x00\x01", i)) >= 0:
        types.append((data[j + 3] >> 1) & 0x3F)
        i = j + 3
    return types


class H265Encoder:
    """1 フレームずつ H.265 に圧縮する。encode() は「Annex B のバイト列, IDR かどうか」を返す。"""

    def __init__(self) -> None:
        self._codec = av.CodecContext.create("libx265", "w")
        self._codec.width = VIDEO_WIDTH
        self._codec.height = VIDEO_HEIGHT
        self._codec.pix_fmt = "yuv420p"
        self._codec.time_base = Fraction(1, VIDEO_RATE_HZ)
        self._codec.framerate = Fraction(VIDEO_RATE_HZ, 1)
        self._codec.options = {"x265-params": X265_PARAMS, "preset": "ultrafast", "tune": "zerolatency"}
        self._pts = 0

    def encode(self, img: Image.Image) -> tuple[bytes, bool]:
        small = img.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.BILINEAR)
        frame = av.VideoFrame.from_ndarray(np.asarray(small, dtype=np.uint8), format="rgb24").reformat(format="yuv420p")
        frame.pts = self._pts
        packets = self._codec.encode(frame)
        # zerolatency なので、1 フレーム入れると必ず 1 パケット出てくる（出てこなければ設定の誤り）
        if len(packets) != 1:
            raise RuntimeError(f"フレーム {self._pts} の圧縮データが {len(packets)} 個出てきました（低遅延の設定を確認）")
        self._pts += 1
        packet = packets[0]
        return bytes(packet), bool(packet.is_keyframe)

    def close(self) -> None:
        """残りのデータを出し切る（zerolatency なので通常は何も出ない）。"""
        rest = self._codec.encode(None)
        if rest:
            raise RuntimeError(f"終了時に圧縮データが {len(rest)} 個残っていました")
