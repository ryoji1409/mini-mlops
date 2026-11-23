"""Parquet の書き出し（ファイル名を決め打ちにして、再実行しても同じ結果になるようにする）。

Ray Data の write_parquet はファイル名にランダムな UUID を付けるため、再実行のたびに
別名のファイルが増えてしまう。そこでセッションごとに pyarrow で直接書き、名前は part-00000 からの連番にする。
入力と変換器の版が同じなら、中身もバイト単位で同じになる（マニフェストの sha256 で確認できる）。
"""

from __future__ import annotations

import hashlib

import pyarrow as pa
import pyarrow.parquet as pq

from converter.layout import part_name
from converter.records import WrittenFile
from converter.storage import Location, write_bytes

# フレームの row group（Parquet 内のまとまり）の行数。
# 一部のフレームだけ読むときに、この単位で読み飛ばせる。64 フレーム ≒ 1〜2MB
FRAME_ROW_GROUP = 64


def _parquet_bytes(table: pa.Table, is_frames: bool) -> bytes:
    """テーブルを Parquet のバイト列にする（いったんメモリ上に作り、sha256 を計算してから 1 回で PUT する）。"""
    sink = pa.BufferOutputStream()
    if is_frames:
        # JPEG はすでに圧縮済みなので、Parquet の圧縮はかけない（CPU の無駄になるだけ）。他の列は zstd
        compression = {name: ("NONE" if name == "jpeg" else "ZSTD") for name in table.column_names}
        pq.write_table(
            table,
            sink,
            compression=compression,
            use_dictionary=["device_id", "session_id", "format"],  # 同じ値が続く列だけ辞書圧縮
            row_group_size=FRAME_ROW_GROUP,
        )
    else:
        pq.write_table(table, sink, compression="ZSTD")
    return sink.getvalue().to_pybytes()


def write_parquet(derived: Location, rel_path: str, table: pa.Table, is_frames: bool = False) -> WrittenFile:
    """1 ファイル書いて、マニフェスト用の情報（行数・サイズ・sha256）を返す。"""
    data = _parquet_bytes(table, is_frames)
    write_bytes(derived, rel_path, data)
    return WrittenFile(
        path=derived.uri_of(rel_path),
        rows=table.num_rows,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def split_by_bytes(table: pa.Table, column: str, max_bytes: int) -> list[pa.Table]:
    """指定列（JPEG）の合計サイズが max_bytes を超えないように、行の順番を保ったまま分割する。

    1 行だけで上限を超える場合も、その行は 1 ファイルに入れる（行を途中で切ることはできないため）。
    """
    sizes = [len(v) for v in table.column(column).to_pylist()]
    shards: list[pa.Table] = []
    start, acc = 0, 0
    for i, size in enumerate(sizes):
        if acc + size > max_bytes and i > start:
            shards.append(table.slice(start, i - start))
            start, acc = i, 0
        acc += size
    if table.num_rows > start or not shards:
        shards.append(table.slice(start))
    return shards


def write_dataset(derived: Location, out_dir: str, table: pa.Table, is_frames: bool, max_bytes: int) -> list[WrittenFile]:
    """1 セッション・1 データセット分を書く。行が 0 ならファイルは作らない。"""
    if table.num_rows == 0:
        return []
    shards = split_by_bytes(table, "jpeg", max_bytes) if is_frames else [table]
    return [write_parquet(derived, f"{out_dir}/{part_name(i)}", shard, is_frames) for i, shard in enumerate(shards)]
