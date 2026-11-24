"""チェックポイント（torchrun 版）。エポックごとに保存し、途中から再開できるようにする。

置き場所: <checkpoint_root>/<ジョブ名>/epoch-0003.pt と latest.json（最新のエポックを指す）
中身: モデルとオプティマイザの状態、終わったエポック数、MLflow の run_id（再開したとき同じ run に記録を続けるため）

Ray Train 版は、チェックポイントの置き場所と再開を Ray Train が管理する（ray_train.py）。
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import torch

from trainer.storage import exists, read_bytes, split_uri, write_bytes

log = logging.getLogger(__name__)


def _location(checkpoint_root: str, job_name: str):
    loc, prefix = split_uri(checkpoint_root.rstrip("/"))
    return loc, f"{prefix}/{job_name}" if prefix else job_name


def save(checkpoint_root: str, job_name: str, state: dict[str, Any]) -> str:
    """状態を保存し、latest.json を更新する（ファイルを書いてから latest.json を書くので、壊れた最新版を指さない）。"""
    loc, prefix = _location(checkpoint_root, job_name)
    rel = f"{prefix}/epoch-{state['epoch']:04d}.pt"
    buf = io.BytesIO()
    torch.save(state, buf)
    write_bytes(loc, rel, buf.getvalue())
    write_bytes(loc, f"{prefix}/latest.json", json.dumps({"epoch": state["epoch"], "file": rel.rsplit("/", 1)[1]}).encode())
    return loc.uri_of(rel)


def load_latest(checkpoint_root: str, job_name: str) -> dict[str, Any] | None:
    """最新のチェックポイントを読む。無ければ None（最初から学習する）。"""
    loc, prefix = _location(checkpoint_root, job_name)
    if not exists(loc, f"{prefix}/latest.json"):
        return None
    latest = json.loads(read_bytes(loc, f"{prefix}/latest.json"))
    rel = f"{prefix}/{latest['file']}"
    state = torch.load(io.BytesIO(read_bytes(loc, rel)), map_location="cpu", weights_only=False)
    log.info("チェックポイントから再開します: %s（%d エポック終了済み）", loc.uri_of(rel), state["epoch"])
    return state
