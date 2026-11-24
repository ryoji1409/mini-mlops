"""TrainJob の完了を待つ（ワークフローの学習ステップ）。

Argo の resource テンプレートの成功条件は「status.xxx == 値」の形しか書けず、TrainJob の
status.conditions（リストの中の type == Complete を探す）を直接は判定できない。
そこで、Kubernetes の API を直接呼んで、TrainJob の conditions を一定間隔で見張る小さなコマンドにした。
（Pod に付く ServiceAccount のトークンで認証する。標準ライブラリだけで書いている）

終了コード: Complete → 0、Failed → 1、時間切れ → 1
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def _get(path: str) -> dict[str, Any]:
    host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    req = urllib.request.Request(f"https://{host}:{port}{path}", headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.load(resp)


def _summary(obj: dict[str, Any]) -> str:
    """状態の要約（変わったときだけログに出す）。"""
    status = obj.get("status", {})
    conds = ",".join(f"{c['type']}={c['status']}" for c in status.get("conditions", []))
    jobs = ";".join(f"{j.get('name')}: active={j.get('active', 0)} ready={j.get('ready', 0)} succeeded={j.get('succeeded', 0)} failed={j.get('failed', 0)}" for j in status.get("jobsStatus", []))
    return f"suspend={obj['spec'].get('suspend')} conditions[{conds}] jobs[{jobs}]"


def wait_trainjob(namespace: str, name: str, timeout_seconds: int, interval: int) -> int:
    path = f"/apis/trainer.kubeflow.org/v1alpha1/namespaces/{namespace}/trainjobs/{name}"
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        obj = _get(path)
        summary = _summary(obj)
        if summary != last:
            # Kueue で待っている間は suspend=True。受け入れられると False になり、Pod が作られる
            log.info("%s/%s: %s", namespace, name, summary)
            last = summary
        conds = {c["type"]: c for c in obj.get("status", {}).get("conditions", [])}
        if conds.get("Complete", {}).get("status") == "True":
            log.info("TrainJob %s が完了しました", name)
            return 0
        if conds.get("Failed", {}).get("status") == "True":
            log.error("TrainJob %s が失敗しました: %s", name, conds["Failed"].get("message"))
            return 1
        time.sleep(interval)
    log.error("TrainJob %s が %d 秒以内に終わりませんでした", name, timeout_seconds)
    return 1
