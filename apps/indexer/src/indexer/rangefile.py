"""S3（またはローカル）のファイルを、範囲指定（Range GET）で読むためのクラス。

S3 では、ファイルの一部だけを「Range: bytes=開始-終了」を付けた GET で取り出せる。
データが大きいほど、MCAP をまるごとダウンロードせず、必要な部分（summary や 1 GOP 分のチャンク）だけを取ることが重要。

このクラスは:
- read_range(offset, length): 指定した範囲だけを読む（pyarrow の read_at = S3 の Range GET）
- 読んだバイト数と回数を数える（「どれだけ読んだか」をログに出すため。スレッドから同時に呼ばれても正しく数える）
- 人工的な遅延（latency_ms）を 1 回の読み出しごとに入れられる（S3 の遅さを真似て、先読みの効果を比べるため）
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from indexer.storage import split_uri


@dataclass
class ReadStats:
    """読み出しの集計。複数の RangeReader で共有できる。"""

    bytes_read: int = 0
    requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, nbytes: int) -> None:
        with self._lock:
            self.bytes_read += nbytes
            self.requests += 1

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.bytes_read, self.requests


class RangeReader:
    """1 つのファイルを範囲指定で読む。"""

    def __init__(self, uri: str, stats: ReadStats | None = None, latency_ms: float = 0.0) -> None:
        self.uri = uri
        loc, rel = split_uri(uri)
        # open_input_file は中身を読まずに開く（S3 では HEAD でサイズだけ取る）。read_at のたびに Range GET する
        self._file = loc.fs.open_input_file(loc.path(rel))
        self.size = self._file.size()
        self.stats = stats or ReadStats()
        self.latency_s = latency_ms / 1000.0
        self._lock = threading.Lock()

    def read_range(self, offset: int, length: int) -> bytes:
        if self.latency_s:
            time.sleep(self.latency_s)  # S3 の 1 回あたりの遅さを真似る（ローカルや近い S3 では速すぎて差が見えないため）
        with self._lock:  # 同じファイルを複数スレッドから読むときの保護
            data = self._file.read_at(length, offset)
        self.stats.add(len(data))
        return data

    def close(self) -> None:
        self._file.close()
