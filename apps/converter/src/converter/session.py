"""1 セッション分の変換（デコード → テーブル作成 → 書き込み → マニフェスト）。

書き込みの順番（途中で落ちても「中途半端な状態」を完了扱いにしないため）:
1. トピックごとの件数が metadata.json と一致するか確かめてから、
   メッセージをデコードして、全データセットのテーブルをメモリ上に作る
   （ここで失敗した場合は、まだストレージに何も触っていない）
2. マニフェストを消す        … ここから先で落ちると「マニフェスト無し = 未処理」になり、次回やり直される
3. 新しい出力を書く          … ファイル名は決まっている（part-00000 …）ので、前回の同名ファイルは上書きされる
4. 今回書かなかった古いファイルを消す（前回の残骸・シャード数が減った分など）
5. 最後にマニフェストを書く  … これで完了
6. 隔離記録があれば消す（以前失敗したが、今回は成功した場合）

「先に全部消してから書く」にしないのは、pyarrow の S3 実装が、ファイルを消して空になった
ディレクトリに 0 バイトの「ディレクトリの印」（キーが / で終わるオブジェクト）を作るため。
上書き → 不要分だけ削除、の順にすればディレクトリが空にならず、印が増えない。
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from converter.config import Settings
from converter.decode import message_class
from converter.layout import DATASETS, SessionRef, manifest_key, output_dir, quarantine_key
from converter.preflight import TopicSchema
from converter.records import WrittenFile, build_manifest
from converter.storage import Location, delete_file, list_files, write_json
from converter.transform import BUILDERS
from converter.writer import write_dataset

log = logging.getLogger(__name__)


class CountMismatchError(Exception):
    """受け取ったメッセージ数が metadata.json と合わない（入力ではなく処理側の問題）。

    事前チェックで「MCAP の統計 = metadata.json」を確認済みなので、ここで合わないのは
    Ray のワーカーが落ちてタスクが再実行された際に行が重複・欠落した、などの処理側の問題。
    （実際に、メモリ不足でワーカーが落ちたときに行が重複したまま渡されたことがある）
    入力は壊れていないので隔離はせず、何も書かずに失敗として報告する（次回の実行でやり直す）。
    """


def _sorted_messages(messages: pa.Table) -> pa.Table:
    """トピック → 記録時刻 → 連番の順に並べる。

    Ray Data の groupby の後は行の順番が保証されないので、ここで並べ直す。
    並びが決まれば出力ファイルの中身も毎回同じになる（再実行しても結果が変わらない）。
    """
    return messages.sort_by([("topic", "ascending"), ("log_time", "ascending"), ("sequence", "ascending")])


def _decode_topic(messages: pa.Table, topic: str, schema: TopicSchema) -> tuple[list[Any], list[int]]:
    """1 トピック分のメッセージをデコードする。戻り値は (メッセージ, 記録時刻) の 2 つのリスト。"""
    rows = messages.filter(pc.equal(messages.column("topic"), topic))
    cls = message_class(schema)
    msgs = [cls.FromString(b) for b in rows.column("data").to_pylist()]
    log_times = [int(t) for t in rows.column("log_time").to_pylist()]
    return msgs, log_times


def build_tables(ref: SessionRef, schemas: dict[str, TopicSchema], messages: pa.Table) -> dict[str, pa.Table]:
    """全データセットのテーブルを作る（ストレージには触らない）。"""
    messages = _sorted_messages(messages)
    tables: dict[str, pa.Table] = {}
    for topic, spec in DATASETS.items():
        if topic not in schemas:
            continue  # このセッションにはこのトピックが無い
        msgs, log_times = _decode_topic(messages, topic, schemas[topic])
        tables[spec.name] = BUILDERS[spec.name](ref, msgs, log_times)
    return tables


def count_topics(messages: pa.Table) -> dict[str, int]:
    """トピックごとのメッセージ数（変換対象外のトピックも含む）。"""
    return dict(Counter(messages.column("topic").to_pylist()))


def _delete_stale(derived: Location, ref: SessionRef, keep_uris: set[str]) -> None:
    """このセッションの出力ディレクトリにある、今回書いたもの以外のファイルを消す。"""
    for spec in DATASETS.values():
        for rel in list_files(derived, output_dir(spec, ref)):
            if derived.uri_of(rel) not in keep_uris:
                derived.fs.delete_file(derived.path(rel))
                log.info("%s: 古いファイルを削除しました: %s", ref.session_id, rel)


def convert_session(
    settings: Settings,
    raw: Location,
    derived: Location,
    ref: SessionRef,
    schemas: dict[str, TopicSchema],
    messages: pa.Table,
) -> dict[str, Any]:
    """1 セッションを変換して derived に書き、マニフェストを返す。失敗したら例外を投げる（呼び出し側で隔離する）。"""
    # 1. 件数を確かめてから、メモリ上でテーブルを作る
    topic_counts = count_topics(messages)
    if topic_counts != ref.expected():
        raise CountMismatchError(f"受け取ったメッセージ数が metadata.json と違います（実際 {topic_counts} / 期待 {ref.expected()}）")
    tables = build_tables(ref, schemas, messages)
    ignored = {t: n for t, n in topic_counts.items() if t not in DATASETS}

    # 2. マニフェストを消す
    delete_file(derived, manifest_key(ref.session_id))

    # 3. 新しい出力を書く（同名の古いファイルは上書き）
    outputs: dict[str, list[WrittenFile]] = {}
    for spec in DATASETS.values():
        table = tables.get(spec.name)
        is_frames = spec.name.startswith("frames")
        outputs[spec.name] = (
            write_dataset(derived, output_dir(spec, ref), table, is_frames, settings.frame_shard_max_bytes)
            if table is not None
            else []
        )

    # 4. 今回書かなかった古いファイルを消す
    _delete_stale(derived, ref, {f.path for files in outputs.values() for f in files})

    # 5. 最後にマニフェストを書く（= 完了の印）
    manifest = build_manifest(settings, raw, ref, outputs, topic_counts, ignored)
    write_json(derived, manifest_key(ref.session_id), manifest)

    # 6. 以前の隔離記録を消す
    delete_file(derived, quarantine_key(ref.session_id))
    log.info("%s: 変換しました %s", ref.session_id, {k: v["rows"] for k, v in manifest["outputs"].items()})
    return manifest
