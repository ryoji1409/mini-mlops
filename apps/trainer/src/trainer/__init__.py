"""シーン分類（時間帯 3 クラス・天候 2 クラス）の学習（フェーズ 4）。"""

from importlib.metadata import PackageNotFoundError, version

TRAINER_NAME = "trainer"

try:
    # バージョンは pyproject.toml の version を唯一の正とする（イメージのタグも同じ値）
    TRAINER_VERSION = version(TRAINER_NAME)
except PackageNotFoundError:
    TRAINER_VERSION = "0.0.0+unknown"
