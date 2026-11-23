"""MCAP に埋め込まれたスキーマ（FileDescriptorSet）から protobuf をデコードする。

read_mcap が返す data 列は protobuf の生のバイト列のまま（Ray 2.59 時点。JSON だけはデコードされる）。
生成器（apps/mcap-generator）のコードや .proto ファイルに頼らず、MCAP の中のスキーマだけで
デコードするので、本物のデバイスログ（別の生成元）にもそのまま使える。
"""

from __future__ import annotations

from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from converter.preflight import TopicSchema


def message_class(schema: TopicSchema) -> type[Any]:
    """スキーマから protobuf のメッセージクラスを作る。

    FileDescriptorSet には、その型が依存する .proto（google/protobuf/timestamp.proto など）も
    すべて含まれている。依存する側より先に依存される側を登録する必要があるので、
    登録できるものから順に、全部登録できるまで繰り返す。
    """
    fds = descriptor_pb2.FileDescriptorSet.FromString(schema.schema_data)
    pool = descriptor_pool.DescriptorPool()  # スキーマごとに専用のプールを使う（他の型と衝突させない）
    remaining = list(fds.file)
    while remaining:
        progressed = False
        for fd in list(remaining):
            try:
                pool.Add(fd)
            except (TypeError, KeyError):
                continue  # 依存先がまだ登録されていない → 次の周回で再挑戦
            remaining.remove(fd)
            progressed = True
        if not progressed:
            names = [f.name for f in remaining]
            raise ValueError(f"FileDescriptorSet の依存関係を解決できません: {names}")
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(schema.schema_name))


def timestamp_ns(ts: Any) -> int:
    """google.protobuf.Timestamp（秒 + ナノ秒）を、エポックからのナノ秒（int）にする。"""
    return int(ts.seconds) * 1_000_000_000 + int(ts.nanos)
