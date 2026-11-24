"""データセット作成のテスト。ローカルのディレクトリを raw / derived / curated に見立て、
変換器が書くのと同じ形のマニフェストとフレームの Parquet を小さく作って動かす。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

from dataset_builder.build import BuildParams, DatasetConflictError, build_dataset
from dataset_builder.storage import open_location

COMBOS = [(t, w) for t in ("day", "dusk", "night") for w in ("clear", "rain")]


def _jpeg(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(out, format="JPEG")
    return out.getvalue()


def _make_session(raw: Path, derived: Path, device: str, session: str, tod: str, weather: str, n_frames: int) -> None:
    """変換済みの 1 セッション（raw の metadata.json、derived のフレームとマニフェスト）を作る。"""
    part = f"device={device}/date=2026-11-01/session_id={session}"
    (raw / part).mkdir(parents=True)
    (raw / part / "metadata.json").write_text(json.dumps({"conditions": {"time_of_day": tod, "weather": weather}}))
    frames_dir = derived / "frames/front" / part
    frames_dir.mkdir(parents=True)
    table = pa.table({"frame_index": pa.array(range(n_frames), pa.int32()), "jpeg": [_jpeg((10 * i, 50, 90)) for i in range(n_frames)]})
    pq.write_table(table, frames_dir / "part-00000.parquet")
    manifest = {
        "status": "converted",
        "device_id": device,
        "session_id": session,
        "converter": {"version": "0.2.1"},
        "input": {"metadata": f"{raw}/{part}/metadata.json", "mcap": f"{raw}/{part}/{session}.mcap"},
        "outputs": {"frames_front": {"rows": n_frames, "files": [{"path": f"{derived}/frames/front/{part}/part-00000.parquet", "sha256": hashlib.sha256((frames_dir / "part-00000.parquet").read_bytes()).hexdigest()}]}},
        "converted_at": "2026-11-01T00:00:00Z",
    }
    (derived / "_manifests").mkdir(exist_ok=True)
    (derived / "_manifests" / f"{session}.json").write_text(json.dumps(manifest))


@pytest.fixture()
def lake(tmp_path: Path):
    """6 通りの組それぞれに 3 セッション（各 4 フレーム）。"""
    raw, derived, curated = tmp_path / "raw", tmp_path / "derived", tmp_path / "curated"
    n = 0
    for tod, weather in COMBOS:
        for _ in range(3):
            n += 1
            _make_session(raw, derived, f"dev-{n % 2 + 1:03d}", f"s-{n:03d}", tod, weather, 4)
    # 変換されていない（マニフェストの無い）セッションは対象外になる
    (raw / "device=dev-009/date=2026-11-01/session_id=s-999").mkdir(parents=True)
    return open_location(str(raw)), open_location(str(derived)), open_location(str(curated))


def _rows(curated_root: Path, split: str) -> pa.Table:
    return pa.concat_tables([pq.read_table(p) for p in sorted((curated_root / f"scene-cls/v1/{split}").glob("*.parquet"))])


def test_セッション単位で分割され全クラスが検証用に入る(lake) -> None:
    raw, derived, curated = lake
    result = build_dataset(raw, derived, curated, BuildParams(version=1, train_shards=4, val_shards=2, val_per_combo=1))
    assert result.status == "built"
    m = result.manifest
    train, val = set(m["splits"]["train"]["sessions"]), set(m["splits"]["val"]["sessions"])
    # 同じセッションが両方に入らない
    assert not train & val and len(train | val) == 18
    # 組ごとに 1 セッションが検証用 → 検証用に全クラスが入る
    assert len(val) == 6
    assert all(v > 0 for v in m["splits"]["val"]["class_counts"]["time_of_day"].values())
    # 行の中身: 画像は 160x90 に縮小され、ファイルの行のセッションは分割と一致する
    val_rows = _rows(Path(curated.root), "val")
    assert set(val_rows["session_id"].to_pylist()) == val
    with Image.open(io.BytesIO(val_rows["image"][0].as_py())) as img:
        assert img.size == (160, 90)
    assert m["splits"]["train"]["frames"] + m["splits"]["val"]["frames"] == 18 * 4
    assert len(m["splits"]["train"]["files"]) == 4
    # 系譜: derived のマニフェストのパスが記録されている
    assert all(i["derived_manifest"].endswith(f"_manifests/{i['session_id']}.json") for i in m["inputs"])


def test_同じ中身ならスキップし違う中身ならエラー(lake) -> None:
    raw, derived, curated = lake
    first = build_dataset(raw, derived, curated, BuildParams(version=1))
    again = build_dataset(raw, derived, curated, BuildParams(version=1))
    assert again.status == "skipped" and again.dataset_hash == first.dataset_hash
    # 作り方（画像サイズ）を変えて同じバージョン番号で作ろうとすると、上書きせずにエラー
    with pytest.raises(DatasetConflictError):
        build_dataset(raw, derived, curated, BuildParams(version=1, width=80, height=45))


def test_変換し直してもフレームが同じならスキップ_フレームが変わればエラー(lake) -> None:
    raw, derived, curated = lake
    first = build_dataset(raw, derived, curated, BuildParams(version=1))
    # force=true で変換し直した状態: マニフェストの converted_at だけが変わり、フレームのファイルは同じ
    manifest_path = Path(derived.root) / "_manifests" / "s-001.json"
    m = json.loads(manifest_path.read_text())
    m["converted_at"] = "2026-11-25T00:00:00Z"
    manifest_path.write_text(json.dumps(m))
    again = build_dataset(raw, derived, curated, BuildParams(version=1))
    assert again.status == "skipped" and again.dataset_hash == first.dataset_hash
    # フレームの中身が変わった（sha256 が違う）ら、同じバージョン番号では作らずにエラー
    m["outputs"]["frames_front"]["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(m))
    with pytest.raises(DatasetConflictError):
        build_dataset(raw, derived, curated, BuildParams(version=1))


def test_同じ入力と同じseedなら同じデータセットになる(lake, tmp_path: Path) -> None:
    raw, derived, _ = lake
    a = build_dataset(raw, derived, open_location(str(tmp_path / "a")), BuildParams(version=1))
    b = build_dataset(raw, derived, open_location(str(tmp_path / "b")), BuildParams(version=1))
    # パスは出力先で変わるので、ファイルの sha256 で比べる
    sha = lambda r: [f["sha256"] for s in ("train", "val") for f in r.manifest["splits"][s]["files"]]  # noqa: E731
    assert sha(a) == sha(b)
    c = build_dataset(raw, derived, open_location(str(tmp_path / "c")), BuildParams(version=1, seed=7))
    assert sha(c) != sha(a)
