"""動画のサンプル一覧（manifest）を読む IterableDataset（フェーズ 5）。

流れ（1 GOP ごと）:
1. サンプル一覧のシャード（Parquet）を読み、同じ GOP（同じ MCAP・同じチャンク）のサンプルをまとめる
2. その GOP のチャンクだけを S3 の Range GET で取る（MCAP 全体はダウンロードしない）
3. チャンクから動画のメッセージ（H.265）と GNSS のメッセージを取り出す
4. 動画をデコードし（video_decoder.py）、必要なフレームだけを取り出す
5. 同じチャンクの GNSS から位置と向きを推定し、経路ラスタを描く（navi_raster）
→ 1 サンプル = カメラ画像 + 経路ラスタ + 正解値

先読み（prefetch）: 2〜5 を別スレッドで、prefetch 個先の GOP まで先に進めておく。
S3 の応答待ち（ネットワーク）とデコードを、学習側の処理と重ねて待ち時間を隠すため。
prefetch=0 なら先読みしない（1 GOP ずつ順に取ってくる）。

MCAP ごとの summary（チャンネルとスキーマ）と、セッションごとの予定ルートは最初に 1 回だけ読んでキャッシュする。
読んだバイト数は ReadStats に集計する（「MCAP 全体よりずっと少ない」ことをログで示すため）。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from indexer.mcap_chunks import GNSS_TOPIC, VIDEO_TOPIC, McapSummary, message_class, parse_chunk, read_summary
from indexer.rangefile import RangeReader, ReadStats
from navi_raster import RasterSpec, estimate_pose, render
from torch.utils.data import IterableDataset, get_worker_info

from trainer.storage import read_bytes, read_json, split_uri
from trainer.video_decoder import VideoDecoder, make_decoder

log = logging.getLogger(__name__)

GT_COLUMNS = ("time_of_day", "weather", "small_objects_near", "large_objects_ahead")


@dataclass
class _McapCache:
    """MCAP ごとの「開いたファイル・summary・protobuf のクラス」と、セッションごとの予定ルート。スレッドから共有する。"""

    stats: ReadStats
    latency_ms: float
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _files: dict[str, tuple[RangeReader, McapSummary, dict[int, type]]] = field(default_factory=dict)
    _routes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def mcap(self, uri: str) -> tuple[RangeReader, McapSummary, dict[int, type]]:
        with self._lock:
            if uri not in self._files:
                reader = RangeReader(uri, self.stats, self.latency_ms)
                summary = read_summary(reader)
                classes = {}
                for topic in (VIDEO_TOPIC, GNSS_TOPIC):
                    cid = summary.channel_id(topic)
                    if cid is not None:
                        schema = summary.channels[cid][1]
                        classes[cid] = message_class(schema, summary.schemas[schema])
                self._files[uri] = (reader, summary, classes)
            return self._files[uri]

    def route(self, uri: str) -> list[tuple[float, float]]:
        with self._lock:
            if uri not in self._routes:
                data = read_bytes(*split_uri(uri))
                self.stats.add(len(data))
                rows = pq.ParquetFile(pa.BufferReader(data)).read().to_pylist()
                self._routes[uri] = [(r["latitude"], r["longitude"]) for r in sorted(rows, key=lambda r: r["seq"])]
            return self._routes[uri]

    def close(self) -> None:
        for reader, _, _ in self._files.values():
            reader.close()


def _group_by_gop(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """並び順（MCAP の参照位置の順）のまま、同じ GOP のサンプルをまとめる。"""
    groups: list[list[dict[str, Any]]] = []
    for r in rows:
        if groups and (groups[-1][0]["mcap_uri"], groups[-1][0]["chunk_offset"]) == (r["mcap_uri"], r["chunk_offset"]):
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


class VideoManifestDataset(IterableDataset):
    """manifest.json（indexer select が作る）を読み、サンプル（dict）を順に返す。

    返す dict:
        image  … カメラ画像（3×H×W、uint8 のテンソル）
        route  … 経路ラスタ（1×128×128、uint8 のテンソル）
        heading_source … 向きを GNSS / 経路のどちらから決めたか
        segment_id, frame_index, log_time_ns, time_of_day, weather, small_objects_near, large_objects_ahead
    """

    def __init__(
        self,
        manifest_uri: str,
        prefetch: int = 4,
        latency_ms: float = 0.0,
        decoder: str = "pyav",
        raster: RasterSpec = RasterSpec(),
        rank: int = 0,
        world_size: int = 1,
        stats: ReadStats | None = None,
    ) -> None:
        manifest = read_json(*split_uri(manifest_uri))
        if manifest is None:
            raise FileNotFoundError(f"manifest がありません: {manifest_uri}")
        self.manifest = manifest
        self.prefetch = prefetch
        self.latency_ms = latency_ms
        self.decoder_name = decoder
        self.raster = raster
        self.rank, self.world_size = rank, world_size
        self.stats = stats or ReadStats()

    def _my_shards(self) -> list[str]:
        """分散学習のランクと DataLoader のワーカーで、シャードを重ならないように分ける。"""
        info = get_worker_info()
        workers, wid = (info.num_workers, info.id) if info else (1, 0)
        n, k = self.world_size * workers, self.rank * workers + wid
        return [u for i, u in enumerate(self.manifest["shards"]) if i % n == k]

    def _load_gop(self, cache: _McapCache, decoder: VideoDecoder, group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """1 GOP 分: Range GET → 取り出し → デコード → 経路ラスタ。先読みのスレッドで動く。"""
        first = group[0]
        reader, summary, classes = cache.mcap(first["mcap_uri"])
        vid, gid = summary.channel_id(VIDEO_TOPIC), summary.channel_id(GNSS_TOPIC)
        packets, gnss = [], []
        for m in parse_chunk(reader.read_range(first["chunk_offset"], first["chunk_length"])):
            if m.channel_id == vid:
                packets.append(classes[vid].FromString(m.data).data)
            elif m.channel_id == gid:
                fix = classes[gid].FromString(m.data)
                gnss.append((m.log_time, fix.latitude, fix.longitude))
        frames = decoder.decode_gop(packets, "h265", upto=max(r["frame_in_gop"] for r in group))
        route = cache.route(first["route_uri"])
        out = []
        for r in group:
            pose = estimate_pose(gnss, r["log_time_ns"], route)
            out.append(
                {
                    "image": torch.from_numpy(np.ascontiguousarray(frames[r["frame_in_gop"]].transpose(2, 0, 1))),
                    "route": torch.from_numpy(render(route, pose, self.raster)[None]),
                    "heading_source": pose.source,
                    "segment_id": r["segment_id"],
                    "frame_index": r["frame_index"],
                    "log_time_ns": r["log_time_ns"],
                    **{c: r[c] for c in GT_COLUMNS},
                }
            )
        return out

    def _groups(self) -> Iterator[list[dict[str, Any]]]:
        for uri in self._my_shards():
            data = read_bytes(*split_uri(uri))
            self.stats.add(len(data))
            yield from _group_by_gop(pq.ParquetFile(pa.BufferReader(data)).read().to_pylist())

    def __iter__(self) -> Iterator[dict[str, Any]]:
        cache = _McapCache(self.stats, self.latency_ms)
        decoder = make_decoder(self.decoder_name)
        try:
            if self.prefetch <= 0:
                for group in self._groups():
                    yield from self._load_gop(cache, decoder, group)
                return
            # 先読み: 最大 prefetch 個の GOP を同時に取りに行き、元の順番で返す
            with ThreadPoolExecutor(max_workers=self.prefetch, thread_name_prefix="prefetch") as pool:
                pending: deque[Future[list[dict[str, Any]]]] = deque()
                for group in self._groups():
                    pending.append(pool.submit(self._load_gop, cache, decoder, group))
                    if len(pending) >= self.prefetch:
                        yield from pending.popleft().result()
                while pending:
                    yield from pending.popleft().result()
        finally:
            cache.close()
