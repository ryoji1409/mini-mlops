"""変換器の一連の流れのテスト（ローカルのディレクトリを raw / derived に見立てる）。"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from converter.cli import main
from converter.config import S3Settings, Settings
from converter.pipeline import run_convert
from converter.validate import run_validate
from converter.writer import split_by_bytes

BROKEN_ID = "dev-901-20261101T000000Z"


def _settings(raw: Path, derived: Path) -> Settings:
    return Settings(
        raw_uri=str(raw),
        derived_uri=str(derived),
        s3=S3Settings(None, None, None, "us-east-1"),
        converter_version="0.1.0",
        converter_image="test",
        frame_shard_max_bytes=32 * 1024 * 1024,
    )


def _metadata(raw: Path) -> dict[str, dict]:
    return {m["session_id"]: m for m in (json.loads(p.read_text()) for p in raw.rglob("metadata.json"))}


def _output_hashes(derived: Path) -> dict[str, str]:
    """全マニフェストに記録された出力ファイルの sha256（パス → sha256）。"""
    hashes = {}
    for p in (derived / "_manifests").glob("*.json"):
        for ds in json.loads(p.read_text())["outputs"].values():
            hashes.update({f["path"]: f["sha256"] for f in ds["files"]})
    return hashes


def test_convert_quarantine_validate_and_rerun(lake: tuple[Path, Path]) -> None:
    raw, derived = lake
    s = _settings(raw, derived)
    meta = _metadata(raw)
    good = sorted(d for d in meta if d != BROKEN_ID)

    # 1 回目: 正常 2 本は変換、壊れた 1 本は事前チェックで隔離
    report = run_convert(s)
    assert sorted(report.converted) == good
    assert list(report.quarantined) == [BROKEN_ID]
    assert report.quarantined[BROKEN_ID].startswith("preflight")
    q = json.loads((derived / "_quarantine" / f"{BROKEN_ID}.json").read_text())
    assert q["stage"] == "preflight" and q["input"]["sha256"] == meta[BROKEN_ID]["file"]["sha256"]

    # マニフェストの行数 = metadata.json の message_count
    for d in good:
        m = json.loads((derived / "_manifests" / f"{d}.json").read_text())
        expected = {t["topic"]: t["message_count"] for t in meta[d]["topics"]}
        assert m["topic_row_counts"] == expected
        assert m["converter"]["version"] == "0.1.0"
        assert m["input"]["sha256"] == meta[d]["file"]["sha256"]

    # 検証: 隔離があっても既定では成功、--fail-on-quarantine なら失敗
    summary = derived.parent / "summary.json"
    assert run_validate(s, summary_file=summary) == 0
    assert json.loads(summary.read_text()) == {
        "sessions": 3, "ok": 2, "quarantined": 1, "mismatch": 0, "missing": 0, "orphans": 0, "invalid_metadata": 0, "unreadable_datasets": 0,
    }
    assert run_validate(s, fail_on_quarantine=True) == 1

    # 2 回目: すべてスキップ
    hashes = _output_hashes(derived)
    report2 = run_convert(s)
    assert report2.converted == [] and report2.quarantined == {}
    assert report2.skipped == {good[0]: "converted", good[1]: "converted", BROKEN_ID: "quarantined"}

    # --force: 作り直しても出力はバイト単位で同じ（決まったファイル名・決まった並び）
    report3 = run_convert(s, force=True, session_ids={good[0]})
    assert report3.converted == [good[0]]
    assert _output_hashes(derived) == hashes


def test_half_written_state_is_recovered(lake: tuple[Path, Path]) -> None:
    """マニフェストが無く残骸だけあるセッション（途中で落ちた想定）は、残骸を消して作り直す。"""
    raw, derived = lake
    s = _settings(raw, derived)
    run_convert(s)
    hashes = _output_hashes(derived)
    session = sorted(p.stem for p in (derived / "_manifests").glob("*.json"))[0]

    # 途中で落ちた状態を作る: マニフェストを消し、余計なファイルを置く
    (derived / "_manifests" / f"{session}.json").unlink()
    junk = next((derived / "frames" / "front").rglob(f"session_id={session}")) / "part-00009.parquet"
    junk.write_bytes(b"garbage")
    assert run_validate(s) == 1  # 残骸（マニフェストの無い Parquet）があるので NG

    report = run_convert(s)
    assert report.converted == [session]
    assert not junk.exists()
    assert _output_hashes(derived) == hashes
    assert run_validate(s) == 0


def test_parquet_columns(lake: tuple[Path, Path]) -> None:
    raw, derived = lake
    run_convert(_settings(raw, derived))
    imu = pq.read_table(next((derived / "telemetry" / "imu").rglob("*.parquet")), partitioning=None)
    assert {"device_id", "session_id", "timestamp", "log_time_ns", "orientation_w", "linear_acceleration_z"} <= set(imu.column_names)
    assert imu.schema.field("timestamp").type == pa.timestamp("ns", tz="UTC")
    # 静止していても重力で z 方向に約 9.8 m/s^2 かかる
    assert 9.0 < sum(imu.column("linear_acceleration_z").to_pylist()) / imu.num_rows < 10.6

    gnss = pq.read_table(next((derived / "telemetry" / "gnss").rglob("*.parquet")), partitioning=None)
    assert 34.0 < gnss.column("latitude")[0].as_py() < 36.0

    frames_file = next((derived / "frames" / "front").rglob("*.parquet"))
    frames = pq.read_table(frames_file, partitioning=None)
    assert frames.column("width")[0].as_py() == 640 and frames.column("height")[0].as_py() == 360
    assert frames.column("jpeg")[0].as_py()[:2] == b"\xff\xd8"  # JPEG の先頭マーカー
    assert frames.column("frame_index").to_pylist() == list(range(frames.num_rows))
    # JPEG 列は圧縮なし、他は zstd
    rg = pq.ParquetFile(frames_file).metadata.row_group(0)
    comp = {rg.column(i).path_in_schema: rg.column(i).compression for i in range(rg.num_columns)}
    assert comp["jpeg"] == "UNCOMPRESSED" and comp["width"] == "ZSTD"


def test_frame_shards_split(lake: tuple[Path, Path]) -> None:
    """シャードの上限を小さくすると、part-00000, part-00001 … に分かれ、合計行数は変わらない。"""
    raw, derived = lake
    s = replace(_settings(raw, derived), frame_shard_max_bytes=200 * 1024)
    run_convert(s)
    m = json.loads(next((derived / "_manifests").glob("*.json")).read_text())
    files = m["outputs"]["frames_front"]["files"]
    assert len(files) > 1
    assert [f["path"].rsplit("/", 1)[1] for f in files] == [f"part-{i:05d}.parquet" for i in range(len(files))]
    assert sum(f["rows"] for f in files) == m["topic_row_counts"]["/camera/front/image_compressed"]
    assert run_validate(s) == 0


def test_split_by_bytes() -> None:
    t = pa.table({"jpeg": [b"x" * 10] * 7})
    assert [s.num_rows for s in split_by_bytes(t, "jpeg", 25)] == [2, 2, 2, 1]
    assert [s.num_rows for s in split_by_bytes(t, "jpeg", 5)] == [1] * 7  # 1 行で上限を超えても 1 ファイルに入れる
    assert [s.num_rows for s in split_by_bytes(t.slice(0, 0), "jpeg", 25)] == [0]


def test_cli_discover_counts_pending(lake: tuple[Path, Path], monkeypatch, tmp_path: Path) -> None:
    raw, derived = lake
    monkeypatch.setenv("RAW_URI", str(raw))
    monkeypatch.setenv("DERIVED_URI", str(derived))
    out = tmp_path / "pending"
    assert main(["discover", "--output-file", str(out)]) == 0
    assert out.read_text().strip() == "3"
    assert main(["discover", "--force=false", "--session-id", BROKEN_ID, "--output-file", str(out)]) == 0
    assert out.read_text().strip() == "1"


def test_device_idで対象を絞れる(lake: tuple[Path, Path], monkeypatch, tmp_path: Path) -> None:
    """--device-id で、そのデバイスのセッションだけを数える・変換する・検証する（フェーズ 3 の並行実行用）。"""
    raw, derived = lake
    monkeypatch.setenv("RAW_URI", str(raw))
    monkeypatch.setenv("DERIVED_URI", str(derived))
    out = tmp_path / "pending"
    # lake には dev-001 の正常 2 本と、dev-901 の壊れた 1 本がある
    assert main(["discover", "--device-id", "dev-001", "--output-file", str(out)]) == 0
    assert out.read_text().strip() == "2"
    # Argo のパラメータが空のとき（--device-id=）は全デバイス
    assert main(["discover", "--device-id=", "--output-file", str(out)]) == 0
    assert out.read_text().strip() == "3"

    # dev-001 だけ変換すると、dev-901 には手を付けない
    s = _settings(raw, derived)
    report = run_convert(s, device_ids={"dev-001"})
    assert len(report.converted) == 2 and not report.quarantined
    assert not (derived / "_quarantine").exists()
    # dev-001 に絞った検証は OK。全体の検証は dev-901 が未変換（MISSING）なので NG
    assert run_validate(s, device_ids={"dev-001"}) == 0
    assert run_validate(s) == 1


def test_存在しないデバイスを指定したら失敗する(lake: tuple[Path, Path], monkeypatch, tmp_path: Path) -> None:
    """デバイス ID の打ち間違いで「対象 0 件のまま成功」にならないこと。"""
    import pytest

    from converter.discover import UnknownDeviceError

    raw, derived = lake
    monkeypatch.setenv("RAW_URI", str(raw))
    monkeypatch.setenv("DERIVED_URI", str(derived))
    out = tmp_path / "pending"
    # CLI は 1 行のエラーにして終了コード 1（Argo のステップが失敗する）
    assert main(["discover", "--device-id", "dev-999", "--output-file", str(out)]) == 1
    assert not out.exists()  # 件数は書かない（後続のステップに進ませない）
    assert main(["validate", "--device-id", "dev-999"]) == 1
    # 実在するデバイスと一緒に指定しても、無いものがあれば失敗
    assert main(["discover", "--device-id", "dev-001", "--device-id", "dev-999"]) == 1
    # convert（RayJob の中）も同じ判定で止まる
    with pytest.raises(UnknownDeviceError):
        run_convert(_settings(raw, derived), device_ids={"dev-999"})


def test_count_mismatch_is_not_written(lake: tuple[Path, Path]) -> None:
    """行が重複したメッセージを渡されたら、何も書かずに CountMismatchError（隔離もしない）。"""
    import pyarrow as pa
    import pytest
    import ray

    from converter.discover import list_raw_sessions
    from converter.session import CountMismatchError, convert_session
    from converter.preflight import check_session
    from converter.storage import open_location

    raw, derived = lake
    s = _settings(raw, derived)
    raw_loc, derived_loc = open_location(s.raw_uri, s.s3), open_location(s.derived_uri, s.s3)
    ref = next(r for r in list_raw_sessions(raw_loc)[0] if r.session_id != BROKEN_ID)
    schemas = check_session(raw_loc, ref).schemas
    msgs = pa.concat_tables(
        ray.get(ray.data.read_mcap([raw_loc.path(ref.mcap_key)], include_metadata=False, include_paths=True).to_arrow_refs())
    )
    duplicated = pa.concat_tables([msgs, msgs.slice(0, 10)])
    with pytest.raises(CountMismatchError):
        convert_session(s, raw_loc, derived_loc, ref, schemas, duplicated)
    assert not any(derived.rglob("*.parquet")) and not any(derived.rglob("*.json"))


def test_ワークフローのイメージタグが変換器のバージョンと一致する() -> None:
    """pipelines/mcap-convert.yaml の converter-image のタグ = pyproject.toml の version であること。

    タグは変換器のバージョンとしてマニフェストに記録され、冪等性の判定に使われる。
    version だけ上げてワークフローのタグを上げ忘れると、古いイメージで変換してしまうので、ここで気づけるようにする。
    """
    app_dir = Path(__file__).resolve().parents[1]
    version = tomllib.loads((app_dir / "pyproject.toml").read_text())["project"]["version"]
    workflow = (app_dir.parents[1] / "pipelines" / "mcap-convert.yaml").read_text()
    tags = re.findall(r"mini-mlops-converter:(\S+)", workflow)
    assert tags == [version]
