"""埋め込みモデルの重みを MLflow のモデルレジストリで管理する。

なぜ MLflow に置くか:
- 重みを毎回インターネット（Hugging Face）から取らない。取るのは登録するときの 1 回だけ（ホストで実行）
- 学習したモデル（フェーズ 4）と同じ場所・同じ手順（版とエイリアス）で管理できる
- MLflow サーバーが S3（s3://mlflow）を代わりに読み書きするので、ジョブに s3://mlflow のキーを配らなくてよい

登録するもの（MLflow の run の成果物 model/ の下）:
    config.json, model.safetensors, preprocessor_config.json, tokenizer の各ファイル
    mini-mlops-model.json … Hugging Face のリポジトリ名・リビジョン（コミットのハッシュ）・ファイルごとの sha256
登録した版には、タグ hf_repo / hf_revision を付ける。同じリビジョンの版が既にあれば、登録し直さずにエイリアスだけ付け替える。

使うとき（resolve_model）:
    エイリアス（例: embedding）から版を引き、成果物をキャッシュのディレクトリにダウンロードして、sha256 を確かめてから使う。
    同じ版がキャッシュにあれば、ダウンロードしない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MANIFEST_FILE = "mini-mlops-model.json"
EXPERIMENT = "embedding-models"
# Hugging Face のリポジトリから取るファイル（pytorch_model.bin は model.safetensors と同じ中身なので取らない）
HF_PATTERNS = ["*.json", "*.safetensors", "*.model"]


@dataclass(frozen=True)
class ModelRef:
    """使うモデルの場所と素性。表の名前・検索結果の記録に使う。"""

    path: Path  # 重みのあるローカルのディレクトリ
    name: str  # レジストリでの名前（例: siglip-base-p16-224）
    version: str  # レジストリの版（ローカルのディレクトリを直接使うときは "local"）
    hf_repo: str
    hf_revision: str

    @property
    def short_revision(self) -> str:
        return self.hf_revision[:7]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_manifest(model_dir: Path, hf_repo: str, hf_revision: str) -> dict[str, Any]:
    """ディレクトリの中のファイルごとの sha256 を、mini-mlops-model.json に書く。"""
    files = {p.name: {"size": p.stat().st_size, "sha256": _sha256(p)} for p in sorted(model_dir.iterdir()) if p.is_file() and p.name != MANIFEST_FILE}
    manifest = {"hf_repo": hf_repo, "hf_revision": hf_revision, "files": files}
    (model_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_manifest(model_dir: Path) -> dict[str, Any]:
    """mini-mlops-model.json と、実際のファイルの sha256 が一致するか確かめる（壊れた・途中までのダウンロードを使わない）。"""
    manifest = json.loads((model_dir / MANIFEST_FILE).read_text())
    for name, info in manifest["files"].items():
        path = model_dir / name
        if not path.exists() or path.stat().st_size != info["size"] or _sha256(path) != info["sha256"]:
            raise RuntimeError(f"モデルのファイルが壊れているか足りません: {path}")
    return manifest


def local_model(model_dir: Path) -> ModelRef:
    """MLflow を通さず、ローカルのディレクトリのモデルを使う（テストや、ネットワークの無い環境で試すとき）。"""
    manifest = verify_manifest(model_dir)
    return ModelRef(model_dir, model_dir.name, "local", manifest["hf_repo"], manifest["hf_revision"])


def download_from_hf(hf_repo: str, hf_revision: str, out_dir: Path) -> Path:
    """Hugging Face から、リビジョン（コミットのハッシュ）を固定して重みを取る。登録のときだけ使う。"""
    from huggingface_hub import snapshot_download

    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=hf_repo, revision=hf_revision, allow_patterns=HF_PATTERNS, local_dir=out_dir)
    # snapshot_download がダウンロードの記録用に作る .cache は登録しない
    shutil.rmtree(out_dir / ".cache", ignore_errors=True)
    write_manifest(out_dir, hf_repo, hf_revision)
    return out_dir


def _find_version(client: Any, name: str, hf_revision: str) -> Any | None:
    """同じリビジョンで登録済みの版を探す（登録をやり直しても、版を増やさないため）。"""
    for mv in client.search_model_versions(f"name = '{name}'"):
        if mv.tags.get("hf_revision") == hf_revision:
            return mv
    return None


def _put_in_cache(src: Path, cache_dir: Path, name: str, version: str) -> None:
    """登録に使った重みを、ホストのキャッシュにも置く（すぐ後の検索で、MLflow から同じものをダウンロードし直さないため）。"""
    target = cache_dir / name / f"v{version}"
    if (target / MANIFEST_FILE).exists():
        return
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    tmp.rename(target)
    log.info("重みをキャッシュにも置きました: %s", target)


def register_model(
    hf_repo: str, hf_revision: str, name: str, alias: str, model_dir: Path | None = None, cache_dir: Path | None = None
) -> dict[str, Any]:
    """重みを MLflow に登録して、エイリアスを付ける。model_dir を渡すと、Hugging Face から取らずにそのディレクトリを登録する。
    cache_dir を渡すと、登録した重みをそのキャッシュにも置く（resolve_model が使う場所）。"""
    import mlflow
    from mlflow import MlflowClient
    from mlflow.exceptions import MlflowException

    client = MlflowClient()
    try:
        client.create_registered_model(name, description="画像・テキストの埋め込みモデル（apps/embedder が使う）")
    except MlflowException:
        pass  # 登録済みのモデル名ならそのまま使う

    mv = _find_version(client, name, hf_revision)
    if mv is not None:
        log.info("%s のリビジョン %s は版 %s として登録済みです（重みは登録し直しません）", name, hf_revision[:7], mv.version)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            src = model_dir or download_from_hf(hf_repo, hf_revision, Path(tmp) / "model")
            if model_dir is not None and verify_manifest(model_dir)["hf_revision"] != hf_revision:
                raise ValueError(f"{model_dir} の重みはリビジョン {hf_revision} ではありません（mini-mlops-model.json を確認）")
            mlflow.set_experiment(EXPERIMENT)
            with mlflow.start_run(run_name=f"{name}-{hf_revision[:7]}") as run:
                mlflow.log_params({"hf_repo": hf_repo, "hf_revision": hf_revision})
                log.info("重みを MLflow に送ります（数百 MB。サーバー経由で s3://mlflow に入る）")
                mlflow.log_artifacts(str(src), "model")
            mv = client.create_model_version(
                name, source=f"{run.info.artifact_uri}/model", run_id=run.info.run_id, tags={"hf_repo": hf_repo, "hf_revision": hf_revision}
            )
            log.info("%s を版 %s として登録しました（リビジョン %s）", name, mv.version, hf_revision[:7])
            if cache_dir is not None:
                _put_in_cache(src, cache_dir, name, str(mv.version))
    client.set_registered_model_alias(name, alias, mv.version)
    return {"name": name, "version": str(mv.version), "alias": alias, "hf_repo": hf_repo, "hf_revision": hf_revision}


def resolve_model(name: str, alias: str, cache_dir: Path) -> ModelRef:
    """エイリアスから版を引き、キャッシュ（なければ MLflow からダウンロード）の重みを返す。"""
    import mlflow
    from mlflow import MlflowClient
    from mlflow.exceptions import MlflowException

    try:
        mv = MlflowClient().get_model_version_by_alias(name, alias)
    except MlflowException as e:
        raise RuntimeError(f"MLflow に {name}@{alias} がありません（先に embedder register-model を実行する）: {e.message}") from e
    target = cache_dir / name / f"v{mv.version}"
    if (target / MANIFEST_FILE).exists():
        try:
            manifest = verify_manifest(target)
            log.info("キャッシュの重みを使います: %s 版 %s（%s）", name, mv.version, target)
            return ModelRef(target, name, str(mv.version), manifest["hf_repo"], manifest["hf_revision"])
        except RuntimeError as e:
            log.warning("%s。ダウンロードし直します", e)
    shutil.rmtree(target, ignore_errors=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    log.info("MLflow から重みをダウンロードします: %s 版 %s（%s）", name, mv.version, mv.source)
    path = Path(mlflow.artifacts.download_artifacts(artifact_uri=mv.source, dst_path=str(tmp)))
    manifest = verify_manifest(path)
    if manifest["hf_revision"] != mv.tags.get("hf_revision"):
        raise RuntimeError(f"登録の記録（{mv.tags.get('hf_revision')}）と重みのリビジョン（{manifest['hf_revision']}）が違います")
    path.rename(target)  # 確かめ終わってから正式な場所に置く（途中で止まっても、壊れた重みを使わない）
    shutil.rmtree(tmp, ignore_errors=True)
    return ModelRef(target, name, str(mv.version), manifest["hf_repo"], manifest["hf_revision"])
