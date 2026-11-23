"""テストの共通準備。

生成器（apps/mcap-generator）で短いセッションを作り、ローカルのディレクトリを raw / derived に見立てて動かす。
生成器は依存の版（protobuf など）が変換器と違うので、同じ環境には入れず `uv run` で別プロセスとして呼ぶ。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import ray

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parents[1]
GENERATOR_DIR = REPO_ROOT / "apps" / "mcap-generator"
BROKEN_SCRIPT = REPO_ROOT / "scripts" / "lib" / "make_broken_session.py"


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """正常なセッション 2 本（各 3〜4 秒）を生成したディレクトリ。pytest の実行中に 1 回だけ作る。"""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv が見つからないため、生成器を呼べません")
    out = tmp_path_factory.mktemp("generated")
    subprocess.run(
        [uv, "run", "--project", str(GENERATOR_DIR), "mcap-generate",
         "--devices", "1", "--sessions-per-device", "2", "--min-duration", "3", "--max-duration", "4",
         "--seed", "7", "--out", str(out)],
        check=True,
    )
    return out


@pytest.fixture()
def lake(tmp_path: Path, generated: Path) -> tuple[Path, Path]:
    """テストごとの raw / derived。raw には正常 2 本 + 壊れた 1 本を置く。"""
    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    shutil.copytree(generated, raw)
    derived.mkdir()
    first = next(p.parent for p in sorted(raw.rglob("metadata.json")))
    subprocess.run([sys.executable, "-I", str(BROKEN_SCRIPT), str(first), str(raw)], check=True)
    return raw, derived


@pytest.fixture(scope="session", autouse=True)
def ray_local():
    """テスト中に使うローカルの Ray（CPU 2 つ）。最後に止める。"""
    ray.init(num_cpus=2, include_dashboard=False, ignore_reinit_error=True)
    yield
    ray.shutdown()
