"""合成 MCAP 生成器。

ホスト（クラスタの外の作業用マシン）で実行し、
1 セッション（session）ごとに 1 本の MCAP と metadata.json を出力する。
"""

# 生成器のバージョン。metadata.json に記録して「どの版の生成器で作ったか」を追えるようにする。
# pyproject.toml の version と揃えること。
GENERATOR_NAME = "mcap-generator"
GENERATOR_VERSION = "0.2.0"
