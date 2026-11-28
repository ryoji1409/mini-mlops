"""動画のデコーダ（差し替えられるように、インターフェース + 実装に分ける）。

この環境（CPU のみ）では PyAV（FFmpeg のソフトウェアデコーダ）を使う。
GPU のある環境では、同じインターフェースで NVDEC（GPU に載っている動画デコード専用の回路）を使う実装に差し替える。
学習器のローダー（apps/trainer の video_dataset.py）と埋め込みの生成（apps/embedder）は、デコーダの中身を知らず、decode_gop だけを呼ぶ。

av（PyAV）は indexer の追加の依存（indexer[video]）なので、索引・選別だけのイメージには入らない。
"""

from __future__ import annotations

from typing import Protocol

import av
import numpy as np


class VideoDecoder(Protocol):
    def decode_gop(self, packets: list[bytes], codec: str, upto: int) -> list[np.ndarray]:
        """1 GOP 分の圧縮データ（先頭が IDR）をデコードし、先頭から upto 番目までのフレームを RGB（H×W×3、uint8）で返す。

        upto を指定するのは、GOP の途中までしか要らないとき（例: 先頭フレームだけ）にデコードを早く打ち切るため。
        P フレームは前のフレームを参照するので、k 番目が欲しければ 0〜k 番目を順にデコードする必要がある。
        """
        ...


class PyAVDecoder:
    """PyAV（FFmpeg）での CPU デコード。GOP ごとに新しいデコーダを作る（GOP は単独でデコードできるので状態を持ち越さない）。"""

    name = "pyav"

    def decode_gop(self, packets: list[bytes], codec: str, upto: int) -> list[np.ndarray]:
        ctx = av.CodecContext.create({"h265": "hevc", "h264": "h264"}[codec], "r")
        frames: list[np.ndarray] = []
        for data in packets[: upto + 1]:
            for frame in ctx.decode(av.Packet(data)):
                frames.append(frame.to_ndarray(format="rgb24"))
        # 低遅延の設定で作った動画なので通常は残らないが、デコーダの中に残ったフレームを出し切る
        if len(frames) <= upto:
            frames.extend(f.to_ndarray(format="rgb24") for f in ctx.decode(None))
        return frames[: upto + 1]


def make_decoder(name: str) -> VideoDecoder:
    if name == "pyav":
        return PyAVDecoder()
    raise ValueError(f"未対応のデコーダ: {name}（この環境では pyav のみ。GPU 環境では NVDEC の実装を追加する）")
