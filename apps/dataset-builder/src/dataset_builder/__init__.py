"""学習用データセットの作成（フェーズ 4）。

derived ゾーンの変換済みフレーム（frames/front）と、raw ゾーンの metadata.json（時間帯・天候）を結び付け、
curated ゾーン（s3://curated/scene-cls/v<N>/）に、バージョン付きのデータセットを作る。
"""

from importlib.metadata import PackageNotFoundError, version

BUILDER_NAME = "dataset-builder"

try:
    # バージョンは pyproject.toml の version を唯一の正とする（イメージのタグも同じ値）
    BUILDER_VERSION = version(BUILDER_NAME)
except PackageNotFoundError:  # パッケージとしてインストールせずに動かした場合
    BUILDER_VERSION = "0.0.0+unknown"
