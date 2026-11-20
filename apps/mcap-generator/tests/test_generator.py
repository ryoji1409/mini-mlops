"""生成器のテスト。`uv run pytest` で実行する。

テストを速く終わらせるため、記録時間は 2 秒に短縮している。
"""

import json
from collections import Counter
from datetime import date
from pathlib import Path

from mcap.reader import make_reader

from mcap_generator.scenario import TIME_OF_DAY_CHOICES, WEATHER_CHOICES, make_scenarios
from mcap_generator.writer import TOPICS, partition_dir, sha256_of, write_session


def _scenarios(seed: int = 42, devices: int = 2, sessions: int = 3, duration: int = 2):
    return make_scenarios(devices, sessions, seed, duration, duration, date(2026, 11, 1))


def test_同じシードなら同じ条件になる() -> None:
    assert _scenarios(seed=1) == _scenarios(seed=1)
    assert _scenarios(seed=1) != _scenarios(seed=2)


def test_6セッションなら時間帯と天候の組み合わせが全部1回ずつ出る() -> None:
    combos = {(s.time_of_day, s.weather) for s in _scenarios()}
    assert len(combos) == len(TIME_OF_DAY_CHOICES) * len(WEATHER_CHOICES)


def test_セッションIDは一意() -> None:
    scenarios = _scenarios(devices=3, sessions=10)
    assert len({s.session_id for s in scenarios}) == len(scenarios)


def test_MCAPとmetadataの中身が一致する(tmp_path: Path) -> None:
    scenario = _scenarios()[0]
    meta = write_session(scenario, tmp_path, seed=42)

    session_dir = tmp_path / partition_dir(scenario)
    mcap_path = session_dir / meta["file"]["name"]
    assert json.loads((session_dir / "metadata.json").read_text()) == meta

    # メッセージ数 = 記録秒数 × 周波数
    expected = {spec.topic: scenario.duration_s * spec.rate_hz for spec in TOPICS}
    assert {t["topic"]: t["message_count"] for t in meta["topics"]} == expected

    # MCAP を実際に読み直して、トピック・スキーマ・件数・時刻を確かめる
    with mcap_path.open("rb") as f:
        reader = make_reader(f)
        summary = reader.get_summary()
        assert summary is not None and summary.statistics is not None
        counts = Counter()
        for schema, channel, _message in reader.iter_messages():
            counts[(channel.topic, schema.name)] += 1
        assert summary.statistics.message_start_time == meta["start_time_ns"]
        assert summary.statistics.message_end_time == meta["end_time_ns"]
    assert counts == Counter({(t["topic"], t["schema"]): t["message_count"] for t in meta["topics"]})

    # metadata の sha256・サイズが実ファイルと一致する
    assert meta["file"]["sha256"] == sha256_of(mcap_path)
    assert meta["file"]["size_bytes"] == mcap_path.stat().st_size

    # 一時ファイルが残っていない
    assert sorted(p.name for p in session_dir.iterdir()) == sorted([mcap_path.name, "metadata.json"])


def test_再生成してもバイト単位で同じファイルになる(tmp_path: Path) -> None:
    """アップロードの冪等性（sha256 で重複判定）が成り立つための前提。"""
    scenario = _scenarios()[0]
    a = write_session(scenario, tmp_path / "a", seed=42)
    b = write_session(scenario, tmp_path / "b", seed=42)
    assert a["file"]["sha256"] == b["file"]["sha256"]


def test_パーティションはraw_ゾーンの形式() -> None:
    scenario = _scenarios()[0]
    parts = partition_dir(scenario).parts
    assert parts[0] == f"device={scenario.device_id}"
    assert parts[1] == f"date={scenario.start_time:%Y-%m-%d}"
    assert parts[2] == f"session_id={scenario.session_id}"
