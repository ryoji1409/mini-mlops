"""ストレージ（S3 / ローカル）へのアクセス。変換器・データセット作成と同じ考え方。

pyarrow の FileSystem を使うので、S3 とローカルディレクトリを同じコードで扱える（テストはローカルで動かす）。
パスは「バケット名/キー」の形で扱う。例: s3://curated → root = "curated"
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import pyarrow.fs as pafs


@dataclass(frozen=True)
class Location:
    """「どのファイルシステムの、どのディレクトリか」を表す。"""

    fs: pafs.FileSystem
    root: str  # ファイルシステム上のルート（例: "curated" や "/tmp/xxx/curated"）
    uri: str  # 人が読む・記録する用の URI（例: "s3://curated"）

    def path(self, rel: str) -> str:
        return f"{self.root}/{rel}" if rel else self.root

    def uri_of(self, rel: str) -> str:
        return f"{self.uri}/{rel}"

    def rel_of_uri(self, uri: str) -> str:
        """マニフェストに書かれた URI（例: s3://derived/frames/...）を、このロケーションからの相対パスに戻す。"""
        if not uri.startswith(self.uri + "/"):
            raise ValueError(f"{self.uri} の下のパスではありません: {uri}")
        return uri[len(self.uri) + 1 :]

    def rel_of(self, path: str) -> str:
        return path[len(self.root) + 1 :] if path.startswith(self.root + "/") else path


def _s3_filesystem() -> pafs.S3FileSystem:
    """aws CLI と同じ環境変数（AWS_ENDPOINT_URL など）から、S3 互換ストレージ用の FileSystem を作る。"""
    kwargs: dict[str, Any] = {"region": os.environ.get("AWS_REGION", "us-east-1")}
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        kwargs.update(access_key=os.environ["AWS_ACCESS_KEY_ID"], secret_key=os.environ["AWS_SECRET_ACCESS_KEY"])
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint:
        # pyarrow は「ホスト:ポート」と「http / https」を別々に受け取るので分解する
        parsed = urlparse(endpoint)
        kwargs.update(endpoint_override=parsed.netloc, scheme=parsed.scheme or "https")
    return pafs.S3FileSystem(**kwargs)


def open_location(uri: str) -> Location:
    """URI から Location を作る。s3:// なら S3、それ以外はローカルのディレクトリとして扱う。"""
    if uri.startswith("s3://"):
        return Location(fs=_s3_filesystem(), root=uri.removeprefix("s3://").rstrip("/"), uri=uri.rstrip("/"))
    root = uri.removeprefix("file://").rstrip("/")
    return Location(fs=pafs.LocalFileSystem(), root=root, uri=root)


def exists(loc: Location, rel: str) -> bool:
    return loc.fs.get_file_info(loc.path(rel)).type == pafs.FileType.File


def read_bytes(loc: Location, rel: str) -> bytes:
    with loc.fs.open_input_stream(loc.path(rel)) as f:
        return f.read()


def write_bytes(loc: Location, rel: str, data: bytes) -> None:
    """ファイルを書く。S3 では 1 回の PUT になるので、読む側から「書きかけ」は見えない。"""
    path = loc.path(rel)
    if isinstance(loc.fs, pafs.LocalFileSystem):
        loc.fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
    with loc.fs.open_output_stream(path) as f:
        f.write(data)


def read_json(loc: Location, rel: str) -> dict[str, Any] | None:
    """JSON ファイルを読む。無ければ None。"""
    return json.loads(read_bytes(loc, rel)) if exists(loc, rel) else None


def write_json(loc: Location, rel: str, obj: dict[str, Any]) -> None:
    write_bytes(loc, rel, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def list_files(loc: Location, rel_dir: str, suffix: str = "") -> list[str]:
    """ディレクトリ以下（再帰）のファイルを、ルートからの相対パスで返す（名前順）。"""
    selector = pafs.FileSelector(loc.path(rel_dir), recursive=True, allow_not_found=True)
    infos = loc.fs.get_file_info(selector)
    return sorted(loc.rel_of(i.path) for i in infos if i.type == pafs.FileType.File and i.path.endswith(suffix))


def delete_files(loc: Location, rels: list[str]) -> None:
    for rel in rels:
        loc.fs.delete_file(loc.path(rel))


def split_uri(uri: str) -> tuple[Location, str]:
    """「s3://bucket/a/b/c」を（バケットの Location, "a/b/c"）に分ける。ローカルパスはそのまま 1 つの Location にする。"""
    if uri.startswith("s3://"):
        bucket, _, rest = uri.removeprefix("s3://").partition("/")
        return open_location(f"s3://{bucket}"), rest
    return open_location(uri), ""


def s3_filesystem() -> pafs.FileSystem:
    """Ray Train のチェックポイント置き場・Ray Data の読み込みに渡す FileSystem。"""
    return _s3_filesystem()
