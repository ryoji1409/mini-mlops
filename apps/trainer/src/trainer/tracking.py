"""MLflow への記録。**rank 0 だけ** が記録する（他の rank は何もしない）。

全 rank が記録すると、同じメトリクスが world_size 回書かれたり、run がいくつもできたりするため。
成果物（混同行列の画像・モデル）は、MLflow サーバー経由で s3://mlflow に保存される（プロキシ方式）。
学習コンテナは MLflow の HTTP API を呼ぶだけで、s3://mlflow のキーは持たない。

系譜のタグ（あとからたどるための目印）:
    dataset.*            … データセットのバージョン・マニフェストの場所・ハッシュ
    converter.versions   … そのデータセットの元になった変換器のバージョン
    trainer.image / trainer.version / mini-mlops.engine / mini-mlops.job_name
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any

import torch
from PIL import Image, ImageDraw

from trainer import TRAINER_VERSION
from trainer.engine import EvalSums
from trainer.settings import TrainSettings, trainer_image

log = logging.getLogger(__name__)

JOB_NAME_TAG = "mini-mlops.job_name"
MODEL_URI_TAG = "mini-mlops.model_uri"


def lineage_tags(settings: TrainSettings, manifest: dict[str, Any]) -> dict[str, str]:
    return {
        JOB_NAME_TAG: settings.job_name,
        "mini-mlops.engine": settings.engine,
        "dataset.name": manifest["name"],
        "dataset.version": str(manifest["version"]),
        "dataset.uri": manifest["uri"],
        "dataset.manifest": manifest["uri"] + "/manifest.json",
        "dataset.hash": manifest["dataset_hash"],
        "converter.versions": ",".join(manifest["converter_versions"]),
        "trainer.image": trainer_image(),
        "trainer.version": TRAINER_VERSION,
    }


def render_confusion(sums: EvalSums, labels: dict[str, list[str]]) -> Image.Image:
    """2 つの混同行列（時間帯・天候）を並べた画像を作る。行 = 正解、列 = 予測。色が濃いほど件数が多い。"""
    cell, margin = 56, 70
    mats = [("time_of_day", sums.confusion_tod, labels["time_of_day"]), ("weather", sums.confusion_weather, labels["weather"])]
    width = sum(margin + cell * len(names) for _, _, names in mats) + 20
    img = Image.new("RGB", (width, margin + cell * 3 + 30), "white")
    draw = ImageDraw.Draw(img)
    x0 = 10
    for title, mat, names in mats:
        draw.text((x0 + margin, 8), f"{title} (row=true, col=pred)", fill="black")
        total = max(int(mat.sum()), 1)
        for i, true_name in enumerate(names):
            draw.text((x0, margin + i * cell + cell // 2 - 6), true_name, fill="black")
            for j, pred_name in enumerate(names):
                if i == 0:
                    draw.text((x0 + margin + j * cell + 4, margin - 18), pred_name, fill="black")
                v = int(mat[i, j])
                shade = 255 - int(200 * v / total * len(names))  # 件数が多いほど濃い青
                shade = max(55, min(255, shade))
                box = (x0 + margin + j * cell, margin + i * cell, x0 + margin + (j + 1) * cell - 2, margin + (i + 1) * cell - 2)
                draw.rectangle(box, fill=(shade, shade, 255))
                draw.text((box[0] + 6, box[1] + cell // 2 - 6), str(v), fill="black")
        x0 += margin + cell * len(names) + 10
    return img


class Tracker:
    """MLflow への記録をまとめたクラス。enabled=False（rank 0 以外、MLflow が無い環境）のときは何もしない。"""

    def __init__(self, enabled: bool) -> None:
        # MLFLOW_TRACKING_URI が無ければ記録しない（ホストでのテストなど）
        self.enabled = enabled and bool(os.environ.get("MLFLOW_TRACKING_URI"))
        self.run_id: str | None = None

    def start(self, settings: TrainSettings, manifest: dict[str, Any], world_size: int, resume_run_id: str | None) -> None:
        """run を始める。チェックポイントに run_id があれば、その run に記録を続ける（再開）。"""
        if not self.enabled:
            return
        import mlflow

        mlflow.set_experiment(settings.experiment)
        if resume_run_id:
            run = mlflow.start_run(run_id=resume_run_id)
            log.info("MLflow の run %s に記録を続けます（再開）", resume_run_id)
        else:
            run = mlflow.start_run(run_name=settings.job_name)
            # パラメータは run を新しく作ったときだけ記録する（MLflow のパラメータは後から変えられないため）
            mlflow.log_params(
                {
                    "lr": settings.lr,
                    "batch_size": settings.batch_size,
                    "epochs": settings.epochs,
                    "seed": settings.seed,
                    "world_size": world_size,
                    "engine": settings.engine,
                }
            )
        self.run_id = run.info.run_id
        # 再開したときはジョブ名などのタグを新しい値で上書きする（登録のステップは最新のジョブ名で run を探す）
        mlflow.set_tags(lineage_tags(settings, manifest) | {"mini-mlops.world_size_last": str(world_size)})

    def log_epoch(self, epoch: int, metrics: dict[str, float]) -> None:
        if self.enabled:
            import mlflow

            mlflow.log_metrics(metrics, step=epoch)

    def finish(self, model: torch.nn.Module, sums: EvalSums, labels: dict[str, list[str]]) -> None:
        """最後のエポックの混同行列の画像と、学習済みモデルを記録して、run を閉じる。"""
        if not self.enabled:
            return
        import mlflow
        import mlflow.pytorch

        mlflow.log_image(render_confusion(sums, labels), "confusion_matrix.png")
        m = sums.metrics()
        # 登録の判定に使う値: 2 つのヘッドの正解率のうち低いほう（両方が閾値を超えていることを求める）
        mlflow.log_metric("val_accuracy_min", min(m["val_time_of_day_accuracy"], m["val_weather_accuracy"]))
        # 保存形式は pickle（モデルのクラスを丸ごと保存。読み込むときは trainer パッケージが必要）。
        # MLflow 3.18 の既定の pt2（torch.export で計算グラフを保存）は、入出力の型を TensorSpec で宣言する必要があり、
        # 出力が 2 つ（時間帯・天候）のこのモデルでは手間が大きいので使わない
        model.eval()
        info = mlflow.pytorch.log_model(model, name="model", serialization_format="pickle")
        mlflow.set_tag(MODEL_URI_TAG, info.model_uri)
        log.info("モデルを記録しました: %s", info.model_uri)
        mlflow.end_run()


def confusion_png(sums: EvalSums, labels: dict[str, list[str]]) -> bytes:
    """テスト・確認用: 混同行列の画像を PNG のバイト列にする。"""
    buf = io.BytesIO()
    render_confusion(sums, labels).save(buf, format="PNG")
    return buf.getvalue()
