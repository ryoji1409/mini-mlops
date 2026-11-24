"""系譜をたどる: 登録されたモデル → 学習の run → データセットのマニフェスト → derived のマニフェスト → raw の MCAP。

ホストから実行する想定（MLflow は port-forward、S3 は .env の admin のキー）:
    MLFLOW_TRACKING_URI=http://localhost:5050 trainer trace --model-name scene-classifier --alias candidate

各段のファイルを実際に読んで、記録されたハッシュと照合しながらたどる（パスが書いてあるだけでなく、中身が一致することを確かめる）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from trainer.storage import read_bytes, split_uri


def _read_uri(uri: str) -> bytes:
    loc, rel = split_uri(uri)
    return read_bytes(loc, rel)


def trace(model_name: str, alias: str, version: str | None = None, limit: int = 0) -> dict[str, Any]:
    from mlflow import MlflowClient

    client = MlflowClient()
    mv = client.get_model_version(model_name, version) if version else client.get_model_version_by_alias(model_name, alias)
    run = client.get_run(mv.run_id)
    tags = run.data.tags
    print(f"モデル        {model_name} version {mv.version}（エイリアス {alias if not version else '-'}）")
    print(f"  └ run       {run.info.run_id}（ジョブ {tags.get('mini-mlops.job_name')}、方式 {tags.get('mini-mlops.engine')}、イメージ {tags.get('trainer.image')}）")

    manifest_uri = tags["dataset.manifest"]
    manifest = json.loads(_read_uri(manifest_uri))
    ok_hash = manifest["dataset_hash"] == tags.get("dataset.hash")
    print(f"    └ データセット {manifest_uri}")
    print(f"        dataset_hash {manifest['dataset_hash'][:16]}…（run のタグと{'一致' if ok_hash else '不一致!'}）、"
          f"学習 {manifest['splits']['train']['frames']} / 検証 {manifest['splits']['val']['frames']} フレーム、変換器 {','.join(manifest['converter_versions'])}")

    sessions = []
    inputs = manifest["inputs"][: limit or None]
    for item in inputs:
        data = _read_uri(item["derived_manifest"])
        derived = json.loads(data)
        ok = hashlib.sha256(data).hexdigest() == item["derived_manifest_sha256"]
        print(f"        └ {item['split']:5} {item['derived_manifest']}（sha256 {'一致' if ok else '不一致!'}、変換器 {derived['converter']['version']}）")
        print(f"            └ raw {derived['input']['mcap']}（sha256 {derived['input'].get('sha256', '?')[:16]}…）")
        sessions.append({"session_id": item["session_id"], "derived_manifest_ok": ok, "raw_mcap": derived["input"]["mcap"]})
    if limit and len(manifest["inputs"]) > limit:
        print(f"        …（ほか {len(manifest['inputs']) - limit} セッション。--limit 0 で全部）")
    return {"model_version": mv.version, "run_id": run.info.run_id, "dataset_hash_ok": ok_hash, "sessions": sessions}
