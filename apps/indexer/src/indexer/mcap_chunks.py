"""MCAP の summary とチャンクを扱うモジュール。

MCAP ファイルの構造（必要な部分だけ）:
    [magic][header][チャンク][チャンク]…[データの終わり][summary（スキーマ・チャンネル・チャンクの索引…）][footer][magic]
- footer はファイルの末尾にあり、summary の開始位置が書いてある
- summary の「チャンクの索引（ChunkIndex）」に、各チャンクの開始オフセット・長さ・含まれる時刻の範囲・
  チャンネルごとの位置が書いてある
→ 末尾から footer と summary だけを読めば、ファイルの中身を読まずに「どの時刻のデータがファイルのどこにあるか」が分かる。

チャンクの範囲（chunk_start_offset から chunk_length バイト）は、オペコード（1 バイト）+ 長さ（8 バイト）+ Chunk レコード。
Chunk レコードの中身は zstd で圧縮されたメッセージの並び。この範囲のバイト列だけで、中のメッセージを全部取り出せる。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from mcap.data_stream import ReadDataStream
from mcap.records import Channel, Chunk, ChunkIndex, Footer, Message, Schema
from mcap.stream_reader import StreamReader, breakup_chunk

from indexer.rangefile import RangeReader

MCAP_MAGIC = b"\x89MCAP0\r\n"
# ファイル末尾の固定長部分: Footer レコード（オペコード 1 + 長さ 8 + 中身 20）+ magic 8
TAIL_SIZE = 1 + 8 + 20 + len(MCAP_MAGIC)

VIDEO_TOPIC = "/camera/front/compressed_video"
ROUTE_TOPIC = "/navigation/route"
GNSS_TOPIC = "/gnss"


@dataclass(frozen=True)
class McapSummary:
    """summary から取り出した情報（このリポジトリで使う分だけ）。"""

    channels: dict[int, tuple[str, str]]  # channel_id → (トピック, スキーマ名)
    schemas: dict[str, bytes]  # スキーマ名 → FileDescriptorSet（protobuf の型定義）
    chunk_indexes: list[Any]  # mcap.records.ChunkIndex（開始オフセット順）
    summary_bytes_read: int  # summary を読むのに取ったバイト数

    def channel_id(self, topic: str) -> int | None:
        return next((cid for cid, (t, _) in self.channels.items() if t == topic), None)


def read_summary(reader: RangeReader) -> McapSummary:
    """footer と summary だけを読む（ファイルの本体は読まない）。Range GET は 2 回:

    1. 末尾の固定長部分（footer + magic、37 バイト）→ summary の開始位置が分かる
    2. summary の開始位置からファイルの末尾まで（スキーマ・チャンネル・チャンクの索引など）

    mcap の make_reader にシークできるファイルを渡しても読めるが、先頭の magic を確かめる読み出しが
    バッファの大きさ（例: 64KB）だけ余計にファイルの本体を読んでしまうため、ここでは自分で 2 回に分けて読む。
    """
    before, _ = reader.stats.snapshot()
    tail = reader.read_range(reader.size - TAIL_SIZE, TAIL_SIZE)
    if tail[-len(MCAP_MAGIC) :] != MCAP_MAGIC or tail[0] != 0x02:  # 0x02 = Footer
        raise ValueError(f"MCAP の末尾ではありません（途中で切れたファイル？）: {reader.uri}")
    footer = Footer.read(ReadDataStream(io.BytesIO(tail[9:29])))
    if footer.summary_start == 0:
        raise ValueError(f"summary がありません（索引の無い MCAP）: {reader.uri}")
    section = reader.read_range(footer.summary_start, reader.size - footer.summary_start)
    after, _ = reader.stats.snapshot()

    schema_records: dict[int, Schema] = {}
    channel_records: dict[int, Channel] = {}
    chunks: list[ChunkIndex] = []
    for record in StreamReader(io.BytesIO(section), skip_magic=True).records:
        if isinstance(record, Schema):
            schema_records[record.id] = record
        elif isinstance(record, Channel):
            channel_records[record.id] = record
        elif isinstance(record, ChunkIndex):
            chunks.append(record)
        elif isinstance(record, Footer):
            break
    schemas = {s.name: s.data for s in schema_records.values()}
    channels = {cid: (ch.topic, schema_records[ch.schema_id].name) for cid, ch in channel_records.items()}
    return McapSummary(channels, schemas, sorted(chunks, key=lambda c: c.chunk_start_offset), after - before)


def parse_chunk(chunk_bytes: bytes) -> list[Message]:
    """チャンクの範囲のバイト列（オペコード + 長さ + Chunk レコード）から、メッセージを取り出す。"""
    stream = ReadDataStream(io.BytesIO(chunk_bytes))
    opcode = stream.read1()
    if opcode != 0x06:  # 0x06 = Chunk
        raise ValueError(f"チャンクではありません（オペコード 0x{opcode:02x}）。参照位置がずれている可能性があります")
    length = stream.read8()
    chunk = Chunk.read(ReadDataStream(io.BytesIO(stream.read(length))))
    return [r for r in breakup_chunk(chunk) if isinstance(r, Message)]


def message_class(schema_name: str, schema_data: bytes) -> type[Any]:
    """MCAP に埋め込まれたスキーマ（FileDescriptorSet）から protobuf のクラスを作る（変換器と同じやり方）。

    生成器のコードや .proto に頼らないので、別の生成元の MCAP にもそのまま使える。
    依存する .proto から順に登録する必要があるので、登録できるものから順に、全部登録できるまで繰り返す。
    """
    fds = descriptor_pb2.FileDescriptorSet.FromString(schema_data)
    pool = descriptor_pool.DescriptorPool()
    remaining = list(fds.file)
    while remaining:
        progressed = False
        for fd in list(remaining):
            try:
                pool.Add(fd)
            except (TypeError, KeyError):
                continue
            remaining.remove(fd)
            progressed = True
        if not progressed:
            raise ValueError(f"FileDescriptorSet の依存関係を解決できません: {[f.name for f in remaining]}")
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(schema_name))


def nal_unit_types(data: bytes) -> list[int]:
    """Annex B の H.265 のバイト列に含まれる NAL の種類。32/33/34 = VPS/SPS/PPS、19/20 = IDR。"""
    types, i = [], 0
    while (j := data.find(b"\x00\x00\x01", i)) >= 0:
        types.append((data[j + 3] >> 1) & 0x3F)
        i = j + 3
    return types
