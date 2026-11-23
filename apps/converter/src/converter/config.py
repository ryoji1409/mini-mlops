"""設定（環境変数から読む）。

S3 の接続先・認証情報は aws CLI と同じ標準の環境変数名を使う。
こうしておくと、ホスト（.env）でもクラスタ内（Secret / ConfigMap を envFrom）でも、
別の S3 互換ストレージでも、コードを変えずに接続先だけ差し替えられる。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from converter import CONVERTER_VERSION


@dataclass(frozen=True)
class S3Settings:
    """S3 互換ストレージへの接続設定。"""

    # 例: http://seaweedfs-all-in-one.storage.svc:8333。None なら本物の AWS S3 を使う
    endpoint_url: str | None
    access_key: str | None
    secret_key: str | None
    region: str


@dataclass(frozen=True)
class Settings:
    """変換器全体の設定。"""

    raw_uri: str  # 入力（raw ゾーン）。例: s3://raw  （テストではローカルのディレクトリも可）
    derived_uri: str  # 出力（derived ゾーン）。例: s3://derived
    s3: S3Settings
    converter_version: str
    # 実行中のイメージ名（Argo / RayJob から環境変数で渡す）。マニフェストに記録するだけ
    converter_image: str | None
    # フレームシャード 1 ファイルの上限（バイト）。これを超えたら次のファイルに分ける
    frame_shard_max_bytes: int


def _env(name: str, default: str | None = None) -> str | None:
    """環境変数を読む。空文字は「未設定」と同じに扱う。"""
    value = os.environ.get(name, "")
    return value if value else default


def load_settings() -> Settings:
    """環境変数から Settings を作る。"""
    shard_mb = int(_env("FRAME_SHARD_MAX_MB", "32"))  # type: ignore[arg-type]
    return Settings(
        raw_uri=_env("RAW_URI", "s3://raw").rstrip("/"),  # type: ignore[union-attr]
        derived_uri=_env("DERIVED_URI", "s3://derived").rstrip("/"),  # type: ignore[union-attr]
        s3=S3Settings(
            endpoint_url=_env("AWS_ENDPOINT_URL"),
            access_key=_env("AWS_ACCESS_KEY_ID"),
            secret_key=_env("AWS_SECRET_ACCESS_KEY"),
            region=_env("AWS_REGION", "us-east-1"),  # type: ignore[arg-type]
        ),
        converter_version=CONVERTER_VERSION,
        converter_image=_env("CONVERTER_IMAGE"),
        frame_shard_max_bytes=shard_mb * 1024 * 1024,
    )
