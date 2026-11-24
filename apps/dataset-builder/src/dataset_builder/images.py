"""画像の縮小。学習しやすい大きさ（既定 160x90）の JPEG にする。"""

from __future__ import annotations

import io

from PIL import Image


def resize_jpeg(data: bytes, width: int, height: int, quality: int) -> bytes:
    """JPEG のバイト列を縮小して、JPEG のバイト列で返す。

    元画像（640x360）と同じ 16:9 なので、縦横比は変わらない。
    縮小には BILINEAR を使う（合成画像なので画質の差は小さく、速さを優先）。
    """
    with Image.open(io.BytesIO(data)) as img:
        small = img.convert("RGB").resize((width, height), Image.Resampling.BILINEAR)
    out = io.BytesIO()
    # optimize=False・プログレッシブなし・サブサンプリング固定で、同じ入力なら同じバイト列になるようにする
    small.save(out, format="JPEG", quality=quality, optimize=False, subsampling=2)
    return out.getvalue()
