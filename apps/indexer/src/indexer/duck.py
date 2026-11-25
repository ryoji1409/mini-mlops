"""DuckDB の準備（S3 互換ストレージを読み書きできるようにする）。変換器の validate と同じ設定。"""

from __future__ import annotations

import os

import duckdb


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(use_s3: bool) -> duckdb.DuckDBPyConnection:
    """DuckDB に接続する。use_s3=True なら、aws CLI と同じ環境変数（AWS_*）で S3 を読めるようにする。"""
    con = duckdb.connect()
    if not use_s3:
        return con
    con.execute("INSTALL httpfs")  # イメージ作成時に入れてあれば何もしない
    con.execute("LOAD httpfs")
    opts = [f"REGION {_sql_str(os.environ.get('AWS_REGION', 'us-east-1'))}", "URL_STYLE 'path'"]  # SeaweedFS はパス形式
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        opts += [f"KEY_ID {_sql_str(os.environ['AWS_ACCESS_KEY_ID'])}", f"SECRET {_sql_str(os.environ['AWS_SECRET_ACCESS_KEY'])}"]
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint:
        scheme, _, host = endpoint.partition("://")
        opts += [f"ENDPOINT {_sql_str(host)}", f"USE_SSL {'true' if scheme == 'https' else 'false'}"]
    con.execute(f"CREATE SECRET s3_mini_mlops (TYPE s3, {', '.join(opts)})")
    return con


def create_views(con: duckdb.DuckDBPyConnection, raw_uri: str, derived_uri: str) -> None:
    """選別の SQL から使う表（ビュー）を作る。

    frames   … フレームごとの正解値（raw の ground_truth.parquet。生成時の真値）
    gops     … GOP 索引（derived/index/gops）
    segments … セグメントごとの集計とタグ（derived/tags/segments.parquet。tag を実行した後だけ）
    hive_partitioning = false: パスの device=… / session_id=… を列にしない（ファイルの中に同じ名前の列があるため）
    """
    con.execute(f"CREATE OR REPLACE VIEW frames AS SELECT * FROM read_parquet({_sql_str(raw_uri + '/*/*/*/ground_truth.parquet')}, hive_partitioning = false)")
    con.execute(f"CREATE OR REPLACE VIEW gops AS SELECT * FROM read_parquet({_sql_str(derived_uri + '/index/gops/*/*/*.parquet')}, hive_partitioning = false)")


def create_segments_view(con: duckdb.DuckDBPyConnection, derived_uri: str) -> None:
    con.execute(f"CREATE OR REPLACE VIEW segments AS SELECT * FROM read_parquet({_sql_str(derived_uri + '/tags/segments.parquet')})")
