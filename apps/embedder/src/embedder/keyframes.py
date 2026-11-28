"""GOP 索引（フェーズ 5）から、各 GOP の先頭フレーム（キーフレーム）だけを取り出す。

読み方はフェーズ 5 の学習器のローダーと同じ:
1. MCAP ごとに、summary（チャンネルとスキーマ）を Range GET で 1 回だけ読む（約 12KB）
2. GOP ごとに、索引の「MCAP 参照位置」（chunk_offset / chunk_length）の範囲だけを Range GET で読む
3. チャンクから動画のメッセージを取り出し、先頭の 1 枚（IDR。単独でデコードできる）だけをデコードする
MCAP を丸ごとダウンロードしない。ただし読む単位はチャンク（= 1 GOP）なので、キーフレーム 1 枚でも GOP 1 つ分（約 95KB）を読む。
全部の GOP を埋め込むと、動画入り MCAP のほとんどを少しずつ読むことになるので、--gop-stride N で N 個おきに間引ける。

キーフレームを使う理由: IDR は前のフレームを参照しないので、1 枚だけデコードすればよい（P フレームは前から順にデコードが必要）。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import numpy as np
from indexer.duck import connect, create_views
from indexer.mcap_chunks import VIDEO_TOPIC, McapSummary, message_class, parse_chunk, read_summary
from indexer.rangefile import RangeReader, ReadStats
from indexer.video_decoder import VideoDecoder, make_decoder

# 埋め込みと一緒に表に入れる、正解値の主要項目（フレームごとの値。ground_truth.parquet の列）
GT_COLUMNS = ("time_of_day", "weather", "small_objects_near", "large_objects_ahead")


def list_keyframes(raw_uri: str, derived_uri: str, gop_stride: int = 1) -> list[dict[str, Any]]:
    """索引の GOP ごとに、先頭フレームの正解値を付けた一覧を返す（MCAP の参照位置の順）。

    gop_stride: N なら GOP 番号が N の倍数のものだけ（N 秒に 1 枚）。1 なら全部。
    """
    con = connect(use_s3=any(u.startswith("s3://") for u in (raw_uri, derived_uri)))
    create_views(con, raw_uri, derived_uri)
    cols = ", ".join(f"f.{c}" for c in GT_COLUMNS)
    rel = con.execute(
        f"""
        SELECT g.segment_id, g.session_id, g.device_id, g.gop_index, f.frame_index, f.log_time_ns,
               g.mcap_uri, g.chunk_offset, g.chunk_length, g.mcap_size_bytes, {cols}
        FROM gops AS g
        JOIN frames AS f ON f.session_id = g.session_id AND f.gop_index = g.gop_index AND f.frame_in_gop = 0
        WHERE g.gop_index % ? = 0
        ORDER BY g.mcap_uri, g.chunk_offset
        """,
        [gop_stride],
    )
    names = [d[0] for d in rel.description]
    return [dict(zip(names, row, strict=True)) for row in rel.fetchall()]


class KeyframeReader:
    """MCAP ごとの「開いたファイル・summary・動画のメッセージのクラス」をキャッシュして、キーフレームを読む。スレッドから共有できる。"""

    def __init__(self, decoder: str = "pyav", stats: ReadStats | None = None) -> None:
        self.stats = stats or ReadStats()
        self.decoder: VideoDecoder = make_decoder(decoder)
        self._lock = threading.Lock()
        self._files: dict[str, tuple[RangeReader, int, type]] = {}

    def _open(self, uri: str) -> tuple[RangeReader, int, type]:
        with self._lock:
            if uri not in self._files:
                reader = RangeReader(uri, self.stats)
                summary: McapSummary = read_summary(reader)
                cid = summary.channel_id(VIDEO_TOPIC)
                if cid is None:
                    raise ValueError(f"動画のトピック {VIDEO_TOPIC} がありません: {uri}")
                schema = summary.channels[cid][1]
                self._files[uri] = (reader, cid, message_class(schema, summary.schemas[schema]))
            return self._files[uri]

    def read(self, row: dict[str, Any]) -> np.ndarray:
        """索引の 1 行（GOP）→ 先頭フレーム（RGB、H×W×3）。"""
        reader, cid, cls = self._open(row["mcap_uri"])
        chunk = reader.read_range(row["chunk_offset"], row["chunk_length"])
        packets = [cls.FromString(m.data).data for m in parse_chunk(chunk) if m.channel_id == cid]
        if not packets:
            raise ValueError(f"チャンクに動画がありません: {row['mcap_uri']} @ {row['chunk_offset']}")
        return self.decoder.decode_gop(packets, "h265", upto=0)[0]

    def close(self) -> None:
        for reader, _, _ in self._files.values():
            reader.close()
        self._files.clear()


def iter_keyframes(reader: KeyframeReader, rows: Iterable[dict[str, Any]], prefetch: int = 4) -> Iterator[tuple[dict[str, Any], np.ndarray]]:
    """(行, 画像) を元の順番で返す。prefetch 個先の GOP まで、別スレッドで先に読んでデコードしておく
    （S3 の応答待ちと埋め込みの計算を重ねる。フェーズ 5 のローダーと同じ考え方）。"""
    if prefetch <= 0:
        for row in rows:
            yield row, reader.read(row)
        return
    with ThreadPoolExecutor(max_workers=prefetch, thread_name_prefix="keyframe") as pool:
        pending: deque[tuple[dict[str, Any], Future[np.ndarray]]] = deque()
        for row in rows:
            pending.append((row, pool.submit(reader.read, row)))
            if len(pending) >= prefetch:
                r, f = pending.popleft()
                yield r, f.result()
        while pending:
            r, f = pending.popleft()
            yield r, f.result()
