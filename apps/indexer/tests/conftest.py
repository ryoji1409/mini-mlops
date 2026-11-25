"""テスト用の raw ゾーンを作る。

生成器（apps/mcap-generator）は依存パッケージの版が違う（protobuf など）ので、同じ仮想環境には入れず、
uv で別プロジェクトとして実行する。動画入り 4 セッション（各 20 秒 = 2 セグメント）と、動画なし 1 セッション。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

GENERATOR = Path(__file__).resolve().parents[2] / "mcap-generator"


def _generate(out: Path, *args: str) -> None:
    subprocess.run(
        ["uv", "run", "-q", "--project", str(GENERATOR), "mcap-generate", "--out", str(out), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def zones(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("uv") is None:
        pytest.skip("uv がないので生成器を実行できません")
    root = tmp_path_factory.mktemp("zones")
    raw = root / "raw"
    _generate(raw, "--video", "--first-device", "31", "--devices", "2", "--sessions-per-device", "2", "--min-duration", "20", "--max-duration", "20", "--seed", "5")
    _generate(raw, "--devices", "1", "--sessions-per-device", "1", "--min-duration", "3", "--max-duration", "3", "--seed", "1")
    return {"raw": raw, "derived": root / "derived", "curated": root / "curated"}
