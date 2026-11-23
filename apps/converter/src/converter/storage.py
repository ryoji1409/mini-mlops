"""ストレージ（S3 / ローカル）へのアクセスをまとめたモジュール。

pyarrow の FileSystem を使う。理由:
- Ray Data（read_mcap）にそのまま渡せる（filesystem 引数）
- S3 とローカルディレクトリを同じコードで扱える（テストはローカルで動かす）
- boto3 などの追加の依存が要らない

パスは「バケット名/キー」の形（pyarrow の S3FileSystem の流儀）で扱う。
例: s3://raw → root = "raw"、ファイルは "raw/device=.../x.mcap"
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import pyarrow.fs as pafs

from converter.config import S3Settings


@dataclass(frozen=True)
class Location:
    """「どのファイルシステムの、どのディレクトリか」を表す。Ray のタスクにもそのまま渡せる（pickle 可能）。"""

    fs: pafs.FileSystem
    root: str  # ファイルシステム上のルート（例: "derived" や "/tmp/xxx/derived"）
    uri: str  # 人が読む・記録する用の URI（例: "s3://derived"）

    def path(self, rel: str) -> str:
        """ルートからの相対パスを、ファイルシステム上のパスにする。"""
        return f"{self.root}/{rel}" if rel else self.root

    def uri_of(self, rel: str) -> str:
        """ルートからの相対パスを、記録用の URI にする（マニフェストに書く）。"""
        return f"{self.uri}/{rel}"

    def rel_of(self, path: str) -> str:
        """ファイルシステム上のパスを、ルートからの相対パスに戻す。"""
        return path[len(self.root) + 1 :] if path.startswith(self.root + "/") else path


def make_s3_filesystem(s3: S3Settings) -> pafs.S3FileSystem:
    """S3 互換ストレージ用の FileSystem を作る。エンドポイントは設定で差し替え可能。"""
    kwargs: dict[str, Any] = {"region": s3.region}
    if s3.access_key and s3.secret_key:
        kwargs.update(access_key=s3.access_key, secret_key=s3.secret_key)
    if s3.endpoint_url:
        # pyarrow は「ホスト:ポート」と「http / https」を別々に受け取るので分解する
        parsed = urlparse(s3.endpoint_url)
        kwargs.update(endpoint_override=parsed.netloc, scheme=parsed.scheme or "https")
        # SeaweedFS などはバケットをホスト名に含める形式（virtual-hosted）に未対応のことが多い。
        # pyarrow は endpoint_override 指定時はパス形式（http://host/bucket/key）で送るのでそのままでよい
    return pafs.S3FileSystem(**kwargs)


def open_location(uri: str, s3: S3Settings) -> Location:
    """URI から Location を作る。s3:// なら S3、それ以外はローカルのディレクトリとして扱う。"""
    if uri.startswith("s3://"):
        return Location(fs=make_s3_filesystem(s3), root=uri.removeprefix("s3://").rstrip("/"), uri=uri)
    local_root = uri.removeprefix("file://").rstrip("/")
    return Location(fs=pafs.LocalFileSystem(), root=local_root, uri=local_root)


def exists(loc: Location, rel: str) -> bool:
    """ファイルがあるかどうか。"""
    return loc.fs.get_file_info(loc.path(rel)).type == pafs.FileType.File


def read_bytes(loc: Location, rel: str) -> bytes:
    with loc.fs.open_input_stream(loc.path(rel)) as f:
        return f.read()


def write_bytes(loc: Location, rel: str, data: bytes) -> None:
    """ファイルを書く。S3 では 1 回の PUT になるので、読む側から「書きかけ」は見えない。"""
    path = loc.path(rel)
    if isinstance(loc.fs, pafs.LocalFileSystem):
        # ローカルでは親ディレクトリを作っておく必要がある（S3 には「ディレクトリ」が無いので不要）
        loc.fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
    with loc.fs.open_output_stream(path) as f:
        f.write(data)


def read_json(loc: Location, rel: str) -> dict[str, Any] | None:
    """JSON ファイルを読む。無ければ None。"""
    if not exists(loc, rel):
        return None
    return json.loads(read_bytes(loc, rel))


def write_json(loc: Location, rel: str, obj: dict[str, Any]) -> None:
    """JSON ファイルを書く（日本語のエラーメッセージも読めるよう ensure_ascii=False）。"""
    write_bytes(loc, rel, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def delete_file(loc: Location, rel: str) -> None:
    """ファイルを消す（無ければ何もしない）。"""
    if exists(loc, rel):
        loc.fs.delete_file(loc.path(rel))


def list_files(loc: Location, rel_dir: str, suffix: str = "") -> list[str]:
    """ディレクトリ以下（再帰）のファイルを、ルートからの相対パスで返す（名前順）。"""
    selector = pafs.FileSelector(loc.path(rel_dir), recursive=True, allow_not_found=True)
    infos = loc.fs.get_file_info(selector)
    return sorted(loc.rel_of(i.path) for i in infos if i.type == pafs.FileType.File and i.path.endswith(suffix))

