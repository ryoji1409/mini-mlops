"""1 GOP だけを Range GET してデコードしてみる（decode-gop）。索引の「MCAP 参照位置」が正しいかを手で確かめる道具。

読むのは MCAP の summary（スキーマを知るため）と、指定したチャンクの範囲だけ。
デコードしたフレーム数と、読んだバイト数（MCAP 全体のサイズとの比）をログに出し、先頭と最後のフレームを PNG に保存する。
"""

from __future__ import annotations

import logging
from pathlib import Path

from indexer.mcap_chunks import VIDEO_TOPIC, message_class, nal_unit_types, parse_chunk, read_summary
from indexer.rangefile import RangeReader
from PIL import Image

from trainer.video_decoder import make_decoder

log = logging.getLogger(__name__)


def decode_gop(mcap_uri: str, offset: int, length: int, out_dir: Path | None, decoder: str = "pyav") -> int:
    reader = RangeReader(mcap_uri)
    try:
        summary = read_summary(reader)
        summary_bytes = reader.stats.bytes_read
        vid = summary.channel_id(VIDEO_TOPIC)
        if vid is None:
            raise SystemExit(f"{VIDEO_TOPIC} のない MCAP です: {mcap_uri}")
        schema = summary.channels[vid][1]
        cls = message_class(schema, summary.schemas[schema])
        packets = [cls.FromString(m.data).data for m in parse_chunk(reader.read_range(offset, length)) if m.channel_id == vid]
    finally:
        reader.close()
    if not packets:
        raise SystemExit("この範囲に動画のメッセージがありません（参照位置がずれている可能性）")
    frames = make_decoder(decoder).decode_gop(packets, "h265", upto=len(packets) - 1)
    total = reader.stats.bytes_read
    log.info("先頭のメッセージの NAL: %s（32/33/34 = VPS/SPS/PPS、19/20 = IDR）", nal_unit_types(packets[0])[:5])
    log.info(
        "デコードしたフレーム: %d / %d。読んだバイト数 %d（summary %d + チャンク %d）= MCAP 全体 %d の %.1f%%",
        len(frames), len(packets), total, summary_bytes, total - summary_bytes, reader.size, 100 * total / reader.size,
    )
    if out_dir is not None and frames:
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in (0, len(frames) - 1):
            Image.fromarray(frames[i]).save(out_dir / f"gop-frame-{i:02d}.png")
        log.info("先頭と最後のフレームを %s に保存しました", out_dir)
    return 0 if len(frames) == len(packets) else 1
