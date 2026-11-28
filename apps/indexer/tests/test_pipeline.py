"""索引 → タグ付け → 選別 を、ローカルのディレクトリで通して確かめる。"""

from __future__ import annotations

import json
from pathlib import Path

import av
import pyarrow.parquet as pq
import pytest

from indexer.build_index import build_index
from indexer.mcap_chunks import VIDEO_TOPIC, message_class, parse_chunk, read_summary
from indexer.rangefile import RangeReader
from indexer.select import ManifestConflictError, SelectParams, select_and_curate
from indexer.storage import open_location
from indexer.tag import tag_segments
from indexer.truth import count_segments


def _read(path: Path) -> list[dict]:
    return pq.ParquetFile(path).read().to_pylist()


@pytest.fixture(scope="module")
def indexed(zones: dict[str, Path]) -> dict[str, Path]:
    report = build_index(str(zones["raw"]), str(zones["derived"]))
    assert len(report.indexed) == 4 and report.not_video == 1
    # summary だけを読むので、MCAP 全体よりずっと少ない
    assert report.bytes_read < report.mcap_bytes * 0.1
    return zones


def test_再実行はスキップされる(indexed: dict[str, Path]) -> None:
    report = build_index(str(indexed["raw"]), str(indexed["derived"]))
    assert report.skipped and not report.indexed and report.bytes_read == 0


def test_参照位置の範囲だけでGOPをデコードできる(indexed: dict[str, Path]) -> None:
    rows = [r for f in sorted((indexed["derived"] / "index" / "gops").rglob("*.parquet")) for r in _read(f)]
    assert len(rows) == 4 * 20  # 4 セッション × 20 GOP
    row = rows[7]
    reader = RangeReader(row["mcap_uri"])
    summary = read_summary(reader)
    vid = summary.channel_id(VIDEO_TOPIC)
    _, schema = summary.channels[vid]
    cls = message_class(schema, summary.schemas[schema])
    before = reader.stats.bytes_read
    msgs = [m for m in parse_chunk(reader.read_range(row["chunk_offset"], row["chunk_length"])) if m.channel_id == vid]
    assert reader.stats.bytes_read - before == row["chunk_length"]
    assert msgs[0].log_time == row["log_time_ns"]
    decoder = av.CodecContext.create("hevc", "r")
    frames = [f for m in msgs for f in decoder.decode(av.Packet(cls.FromString(m.data).data))] + list(decoder.decode(None))
    assert len(frames) == row["frames"] == 10


def test_タグ付けと選別の件数が正解値と一致する(indexed: dict[str, Path]) -> None:
    raw, derived, curated = (str(indexed[k]) for k in ("raw", "derived", "curated"))
    segments = tag_segments(open_location(raw), open_location(derived), raw, derived).to_pylist()
    assert len(segments) == 8  # 4 セッション × 2 セグメント
    assert all(s["frames"] == 100 for s in segments)

    sql = indexed["raw"].parent / "all_rain.sql"
    sql.write_text("SELECT segment_id FROM segments WHERE weather = 'rain'")
    m = select_and_curate(raw, derived, open_location(curated), SelectParams("t", 1, str(sql), frames="all", gops_per_shard=3))
    expected, total = count_segments(open_location(raw), None, "rain")
    assert total == 8 and m["counts"]["segments"] == expected
    assert m["counts"]["samples"] == expected * 100  # all: 1 セグメント = 100 フレーム

    shard_rows = [r for u in m["shards"] for r in _read(Path(u))]
    assert len(shard_rows) == m["counts"]["samples"]
    # 1 つの GOP が 2 つのシャードにまたがらない
    gop_shard = {}
    for i, u in enumerate(m["shards"]):
        for r in _read(Path(u)):
            assert gop_shard.setdefault((r["mcap_uri"], r["chunk_offset"]), i) == i

    # 同じ条件の再実行はスキップ、条件を変えて同じバージョンに書くとエラー
    again = select_and_curate(raw, derived, open_location(curated), SelectParams("t", 1, str(sql), frames="all", gops_per_shard=3))
    assert again["content_hash"] == m["content_hash"]
    with pytest.raises(ManifestConflictError):
        select_and_curate(raw, derived, open_location(curated), SelectParams("t", 1, str(sql), frames="first"))


def test_randomは種で決まる(indexed: dict[str, Path]) -> None:
    raw, derived, curated = (str(indexed[k]) for k in ("raw", "derived", "curated"))
    tag_segments(open_location(raw), open_location(derived), raw, derived)
    sql = indexed["raw"].parent / "all.sql"
    sql.write_text("SELECT segment_id FROM segments")
    a = select_and_curate(raw, derived, open_location(curated), SelectParams("r", 1, str(sql), frames="random", seed=1))
    b = select_and_curate(raw, derived, open_location(curated), SelectParams("r", 2, str(sql), frames="random", seed=1))
    c = select_and_curate(raw, derived, open_location(curated), SelectParams("r", 3, str(sql), frames="random", seed=2))
    pick = lambda m: [r["frame_in_gop"] for u in m["shards"] for r in _read(Path(u))]  # noqa: E731
    assert pick(a) == pick(b) != pick(c)
    assert a["counts"]["samples"] == a["counts"]["gops"]


def test_外から渡したsegment_idで絞り込める(indexed: dict[str, Path]) -> None:
    """フェーズ 6 の検索結果（segment_id の一覧）を candidates として渡し、タグの条件と組み合わせる。"""
    import pyarrow as pa

    raw, derived, curated = (str(indexed[k]) for k in ("raw", "derived", "curated"))
    tag_segments(open_location(raw), open_location(derived), raw, derived)
    all_ids = [s["segment_id"] for s in _read(indexed["derived"] / "tags" / "segments.parquet")]
    ids_path = indexed["raw"].parent / "search_result.parquet"
    # 検索結果と同じ形（segment_id のほかに score などの列があってもよい）。索引に無い segment_id は選ばれない
    pq.write_table(pa.table({"segment_id": all_ids[:3] + ["no-such-session_000"], "score": [0.9, 0.8, 0.7, 0.6]}), ids_path)
    m = select_and_curate(raw, derived, open_location(curated), SelectParams("s", 1, "candidates", segment_ids_uri=str(ids_path)))
    assert m["counts"]["segments"] == 3
    assert json.loads((indexed["curated"] / "s" / "v1" / "manifest.json").read_text())["selection"]["segment_ids_uri"] == str(ids_path)
