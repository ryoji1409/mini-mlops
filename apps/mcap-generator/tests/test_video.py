"""--video（フェーズ 5）のテスト。

一番大事な要件: 「MCAP の summary に書かれたチャンクの位置（開始オフセットと長さ）の範囲のバイト列だけで、
その GOP をデコードできる」こと。ファイルの他の部分を読まずに確かめる。
"""

from __future__ import annotations

import io
import math
from datetime import date
from pathlib import Path

import av
import pyarrow.parquet as pq
import pytest
from mcap.reader import make_reader
from mcap.records import Chunk
from mcap.stream_reader import breakup_chunk
from mcap.data_stream import ReadDataStream
from mcap.records import Message

from mcap_generator.ground_truth import FRAMES_PER_SEGMENT, GROUND_TRUTH_FILE
from mcap_generator.scenario import make_scenarios
from mcap_generator.sensors import EARTH_RADIUS_M
from mcap_generator.video import GOP_LENGTH, nal_unit_types
from mcap_generator.writer import partition_dir, sha256_of, write_session

DURATION_S = 25  # 2.5 セグメント（端数のセグメントも作られることを確かめる）


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("video")
    scenario = make_scenarios(1, 1, 31, DURATION_S, DURATION_S, date(2026, 11, 1), first_device=31)[0]
    meta = write_session(scenario, out, seed=31, video=True)
    session_dir = out / partition_dir(scenario)
    return scenario, meta, session_dir / meta["file"]["name"], session_dir


def _video_channel_id(summary) -> int:
    return next(cid for cid, ch in summary.channels.items() if ch.topic == "/camera/front/compressed_video")


def _messages_in_chunk(chunk_bytes: bytes, channel_id: int) -> list[Message]:
    """チャンクの範囲のバイト列（オペコード + 長さ + Chunk レコード）だけから、メッセージを取り出す。"""
    stream = ReadDataStream(io.BytesIO(chunk_bytes))
    stream.read1()  # オペコード（0x06 = Chunk）
    length = stream.read8()
    chunk = Chunk.read(ReadDataStream(io.BytesIO(stream.read(length))))
    return [r for r in breakup_chunk(chunk) if isinstance(r, Message) and r.channel_id == channel_id]


def test_1チャンク1GOPで_チャンクの範囲だけで途中のGOPをデコードできる(session) -> None:
    from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo

    _, meta, mcap_path, _ = session
    data = mcap_path.read_bytes()
    summary = make_reader(io.BytesIO(data)).get_summary()
    vid = _video_channel_id(summary)
    chunks = sorted(summary.chunk_indexes, key=lambda c: c.chunk_start_offset)
    # 先頭はルートだけのチャンク、その後は 1 秒 = 1 チャンク = 1 GOP
    assert len(chunks) == DURATION_S + 1 and vid not in chunks[0].message_index_offsets
    assert all(vid in c.message_index_offsets for c in chunks[1:])

    middle = chunks[len(chunks) // 2]
    piece = data[middle.chunk_start_offset : middle.chunk_start_offset + middle.chunk_length]  # Range GET 相当
    msgs = _messages_in_chunk(piece, vid)
    assert len(msgs) == GOP_LENGTH
    frames_data = [CompressedVideo.FromString(m.data).data for m in msgs]
    # GOP の先頭は VPS(32) / SPS(33) / PPS(34) 付きの IDR、残りは普通のフレーム
    assert nal_unit_types(frames_data[0])[:3] == [32, 33, 34]
    assert all(19 not in nal_unit_types(f) and 20 not in nal_unit_types(f) for f in frames_data[1:])
    # チャンクの最初のメッセージの時刻 = IDR の時刻（索引に「GOP 先頭の時刻」として書ける）
    assert msgs[0].log_time == middle.message_start_time

    decoder = av.CodecContext.create("hevc", "r")
    decoded = [f for d in frames_data for f in decoder.decode(av.Packet(d))] + list(decoder.decode(None))
    assert len(decoded) == GOP_LENGTH and (decoded[0].width, decoded[0].height) == (meta["video"]["width"], meta["video"]["height"])


def test_正解値とセグメント(session) -> None:
    scenario, meta, _, session_dir = session
    # ParquetFile で 1 ファイルだけ読む（read_table はディレクトリ名の session_id=… も列にしようとして、ファイル内の列とぶつかる）
    gt = pq.ParquetFile(session_dir / GROUND_TRUTH_FILE).read().to_pylist()
    assert len(gt) == DURATION_S * 10
    segments = sorted({r["segment_id"] for r in gt})
    assert segments == [f"{scenario.session_id}_{k:03d}" for k in range(3)]  # 25 秒 → 0〜9、10〜19、20〜24 秒
    assert meta["segments"]["count"] == 3 and meta["segments"]["frames_per_segment"] == FRAMES_PER_SEGMENT
    assert all(r["time_of_day"] == scenario.time_of_day and r["weather"] == scenario.weather for r in gt)
    assert {r["frame_in_gop"] for r in gt} == set(range(GOP_LENGTH))
    # 付属ファイルの sha256 が metadata.json に記録されている（アップロードと検証で照合する）
    assert meta["sidecars"] == [{"name": GROUND_TRUTH_FILE, "size_bytes": (session_dir / GROUND_TRUTH_FILE).stat().st_size, "sha256": sha256_of(session_dir / GROUND_TRUTH_FILE)}]


def test_予定ルートとGNSSのコースが整合する(session) -> None:
    from foxglove_schemas_protobuf.LocationFix_pb2 import LocationFix
    from foxglove_schemas_protobuf.LocationFixes_pb2 import LocationFixes

    _, _, mcap_path, _ = session
    route, gnss = [], []
    with mcap_path.open("rb") as f:
        for _, channel, message in make_reader(f).iter_messages(topics=["/navigation/route", "/gnss"]):
            if channel.topic == "/navigation/route":
                route = [(p.latitude, p.longitude) for p in LocationFixes.FromString(message.data).fixes]
            else:
                fix = LocationFix.FromString(message.data)
                gnss.append((fix.latitude, fix.longitude))
    lat0 = route[0][0]
    to_xy = lambda p: (math.radians(p[1]) * EARTH_RADIUS_M * math.cos(math.radians(lat0)), math.radians(p[0]) * EARTH_RADIUS_M)  # noqa: E731
    rxy = [to_xy(p) for p in route]
    # GNSS の各点が、ルートの点のどれかから 5m 以内にある（点の間隔 5m・GNSS の誤差 0.5m）
    worst = max(min(math.dist(to_xy(g), r) for r in rxy) for g in gnss)
    assert worst < 5.0, worst


def test_videoなしの出力は変わらない(tmp_path: Path) -> None:
    scenario = make_scenarios(1, 1, 42, 3, 3, date(2026, 11, 1))[0]
    meta = write_session(scenario, tmp_path, seed=42)
    assert meta["schema_version"] == 1 and "sidecars" not in meta and len(meta["topics"]) == 3
