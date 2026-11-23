"""事前チェック（read_mcap で読む前に、壊れた MCAP を弾く）。

read_mcap は 1 ファイルを 1 つの読み取りタスクで読むので、壊れたファイルが 1 本あると
データセット全体の実行が例外で止まってしまう。そこで先に 1 セッションずつチェックし、
不合格のセッションは隔離して read_mcap の対象から外す。

チェック内容:
1. サイズと sha256 が metadata.json の値と一致するか（転送中の破損・取り違えの検出）
2. MCAP 末尾の summary（チャンクの索引・統計・スキーマ）が読めるか（途中で切れたファイルの検出）
3. summary の統計にあるトピックごとのメッセージ数が metadata.json と一致するか
4. 変換対象トピックのスキーマが、期待する protobuf の型か

ここで集めたスキーマ（FileDescriptorSet）は、変換時の protobuf のデコードに使う。
（read_mcap の include_metadata=True でも取れるが、全行に同じスキーマのバイト列が付いて重いので使わない）

注意: sha256 の計算で MCAP を 1 回全部読むので、read_mcap と合わせて 2 回読むことになる。
1 セッションが数 GB になると読み取り量の倍増が効いてくるので、見直し候補。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from mcap.reader import make_reader

from converter.layout import DATASETS, SessionRef
from converter.storage import Location

_CHUNK = 8 * 1024 * 1024  # sha256 を計算するときに 1 回で読む量（全体をメモリに載せないため）


@dataclass(frozen=True)
class TopicSchema:
    """1 トピック分のスキーマ情報（MCAP の Schema / Channel レコードから取る）。"""

    schema_name: str  # 例: foxglove.CompressedImage
    schema_encoding: str  # 例: protobuf（data は FileDescriptorSet のバイト列）
    schema_data: bytes
    message_encoding: str  # 例: protobuf


@dataclass(frozen=True)
class PreflightResult:
    session_id: str
    ok: bool
    error: str | None = None
    schemas: dict[str, TopicSchema] = field(default_factory=dict)  # トピック → スキーマ


class PreflightError(Exception):
    """事前チェックで不合格になったことを表す例外。"""


def _check_size_and_sha256(raw: Location, ref: SessionRef) -> None:
    path = raw.path(ref.mcap_key)
    size = raw.fs.get_file_info(path).size
    if size != ref.input_size:
        raise PreflightError(f"サイズが metadata.json と違います（実際 {size} / 期待 {ref.input_size}）")
    digest = hashlib.sha256()
    with raw.fs.open_input_stream(path) as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    if digest.hexdigest() != ref.input_sha256:
        raise PreflightError("sha256 が metadata.json と違います（ファイルが壊れているか、別のファイルです）")


def _read_schemas_and_counts(raw: Location, ref: SessionRef) -> tuple[dict[str, TopicSchema], dict[str, int]]:
    """MCAP の summary から、トピックごとのスキーマとメッセージ数を読む。

    open_input_file はランダムアクセス可能なファイルを返すので、
    mcap のリーダーはファイル末尾の summary だけを読みに行ける（全体は読まない）。
    """
    with raw.fs.open_input_file(raw.path(ref.mcap_key)) as f:
        summary = make_reader(f).get_summary()
    if summary is None:
        raise PreflightError("MCAP に summary がありません（書き込みが途中で止まったファイルの可能性）")
    if summary.statistics is None:
        raise PreflightError("MCAP の summary に統計（Statistics）がありません")
    schemas: dict[str, TopicSchema] = {}
    counts: dict[str, int] = {}
    for channel_id, channel in summary.channels.items():
        schema = summary.schemas.get(channel.schema_id)
        if schema is not None:
            schemas[channel.topic] = TopicSchema(schema.name, schema.encoding, schema.data, channel.message_encoding)
        counts[channel.topic] = counts.get(channel.topic, 0) + summary.statistics.channel_message_counts.get(channel_id, 0)
    return schemas, counts


def _check_counts(ref: SessionRef, counts: dict[str, int]) -> None:
    expected = ref.expected()
    if counts != expected:
        raise PreflightError(f"トピックごとのメッセージ数が metadata.json と違います（実際 {counts} / 期待 {expected}）")


def _check_schemas(schemas: dict[str, TopicSchema]) -> None:
    for topic, spec in DATASETS.items():
        s = schemas.get(topic)
        if s is None:
            continue  # トピックが無いセッションは「0 行」として扱う（エラーにはしない）
        if s.schema_encoding != "protobuf" or s.message_encoding != "protobuf":
            raise PreflightError(f"{topic}: protobuf 以外のエンコーディングには未対応です（{s.schema_encoding}/{s.message_encoding}）")
        if s.schema_name != spec.schema_name:
            raise PreflightError(f"{topic}: 型が想定と違います（実際 {s.schema_name} / 期待 {spec.schema_name}）")


def check_session(raw: Location, ref: SessionRef) -> PreflightResult:
    """1 セッション分の事前チェック。例外は投げず、結果オブジェクトで返す（Ray のタスクとして並列に呼ぶため）。"""
    try:
        _check_size_and_sha256(raw, ref)
        schemas, counts = _read_schemas_and_counts(raw, ref)
        _check_counts(ref, counts)
        _check_schemas(schemas)
    except Exception as e:  # noqa: BLE001  どんな例外でも「このセッションは不合格」として扱う
        return PreflightResult(ref.session_id, ok=False, error=f"{type(e).__name__}: {e}")
    return PreflightResult(ref.session_id, ok=True, schemas=schemas)
