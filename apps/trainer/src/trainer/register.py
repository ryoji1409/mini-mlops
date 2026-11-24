"""評価とモデル登録（ワークフローの最後のステップ）。

学習の run（rank 0 が記録したもの）を、ジョブ名のタグで探し、検証データの正解率が閾値を超えていれば、
MLflow のモデルレジストリに登録して、エイリアス（既定 candidate）を付ける。
閾値に届かなければ登録せず、理由をログに出して **正常終了** する（学習自体は成功しているため。ワークフローも成功にする）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from trainer.tracking import JOB_NAME_TAG, MODEL_URI_TAG

log = logging.getLogger(__name__)

# モデルのバージョンにも写しておくタグ（レジストリから系譜をたどれるように）
COPIED_TAGS = ("dataset.name", "dataset.version", "dataset.manifest", "dataset.hash", "converter.versions", "trainer.image", "trainer.version", "mini-mlops.engine", JOB_NAME_TAG)


def find_run(client: Any, experiment: str, job_name: str) -> Any:
    """ジョブ名のタグが付いた run を探す（再開した run は、タグが最新のジョブ名に書き換わっている）。"""
    exp = client.get_experiment_by_name(experiment)
    if exp is None:
        raise RuntimeError(f"MLflow の experiment {experiment} がありません")
    runs = client.search_runs([exp.experiment_id], filter_string=f"tags.`{JOB_NAME_TAG}` = '{job_name}'", order_by=["attributes.start_time DESC"], max_results=1)
    if not runs:
        raise RuntimeError(f"ジョブ {job_name} の run が見つかりません")
    return runs[0]


def register(job_name: str, threshold: float, experiment: str, model_name: str, alias: str, summary_file: Path | None) -> int:
    import mlflow
    from mlflow import MlflowClient

    client = MlflowClient()
    run = find_run(client, experiment, job_name)
    metrics, tags = run.data.metrics, run.data.tags
    acc = metrics.get("val_accuracy_min")
    summary: dict[str, Any] = {
        "run_id": run.info.run_id,
        "val_time_of_day_accuracy": metrics.get("val_time_of_day_accuracy"),
        "val_weather_accuracy": metrics.get("val_weather_accuracy"),
        "val_accuracy_min": acc,
        "threshold": threshold,
        "registered": False,
    }
    if acc is None or MODEL_URI_TAG not in tags:
        log.error("run %s に学習済みモデルか最終の評価がありません（学習が最後まで終わっていない）", run.info.run_id)
        return 1
    if acc < threshold:
        summary["reason"] = f"検証の正解率（2 つのヘッドの低いほう）{acc:.4f} が閾値 {threshold} に届かないため、登録しません"
        log.warning("%s", summary["reason"])
    else:
        mv = mlflow.register_model(tags[MODEL_URI_TAG], model_name, tags={k: tags[k] for k in COPIED_TAGS if k in tags} | {"run_id": run.info.run_id, "val_accuracy_min": f"{acc:.4f}"})
        client.set_registered_model_alias(model_name, alias, mv.version)
        summary.update(registered=True, model_name=model_name, version=mv.version, alias=alias)
        log.info("登録しました: %s version %s（エイリアス %s）正解率 %.4f ≥ 閾値 %s", model_name, mv.version, alias, acc, threshold)
    print(json.dumps(summary, ensure_ascii=False))
    if summary_file:
        summary_file.write_text(json.dumps(summary, ensure_ascii=False) + "\n")
    return 0
