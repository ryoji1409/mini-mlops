"""MCAP → Parquet 変換器（フェーズ 2）。

raw ゾーン（s3://raw）の MCAP を Ray Data の read_mcap で読み、
derived ゾーン（s3://derived）にテレメトリの Parquet・フレームのシャード・系譜マニフェストを書く。
"""

from importlib.metadata import PackageNotFoundError, version

CONVERTER_NAME = "converter"

try:
    # 変換器のバージョンは pyproject.toml の version を唯一の正とする。
    # イメージのタグも同じ値にする（docs/setup/02-pipeline.md 手順 6）ので、
    # 「マニフェストの converter.version = イメージタグ」になる。
    CONVERTER_VERSION = version(CONVERTER_NAME)
except PackageNotFoundError:  # パッケージとしてインストールせずに動かした場合
    CONVERTER_VERSION = "0.0.0+unknown"
