"""ベクトル検索（embedder search）。

テキスト（例: "night"）または画像 1 枚を埋め込みにし、表の中で近い順に並べる（ベクトル検索）。

検索の単位（--level）:
    segments … セグメントの平均ベクトル（segments 表）と比べる。セグメント全体の雰囲気（夜・雨など）に向く
    frames   … キーフレームごと（frames 表）と比べ、セグメントの中で一番近いキーフレームの値をセグメントの値にする。
               セグメントの一部にだけ写るもの（近くの小物体など）に向く
スコア:
    テキスト … SigLIP の「テキストと画像が合っている確率」（0〜1）
    画像     … コサイン類似度（-1〜1。1 に近いほど似ている）
--where で、表の列（正解値・セッションなど）の条件を先に掛けられる（例: "weather = 'rain'"）。

結果は segment_id の一覧（Parquet）として書き出す。indexer select --segment-ids-uri にそのまま渡せる
（select の SQL からは candidates という表として見える。sql/candidates.sql は「渡された一覧をそのまま選ぶ」）。
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from indexer.storage import read_bytes, split_uri, write_bytes
from PIL import Image, ImageDraw

from embedder.keyframes import KeyframeReader
from embedder.model import DEFAULT_PROMPT, SiglipEmbedder
from embedder.tables import FRAMES, SEGMENTS, connect, open_table, table_name

log = logging.getLogger(__name__)

LEVELS = ("segments", "frames")
# 結果に付ける正解値の列（segments 表の列。評価と、目で確かめるときに使う）
TRUTH_COLUMNS = ("time_of_day", "weather", "small_objects_near_max", "small_objects_near_any", "large_objects_ahead_any")


@dataclass(frozen=True)
class Query:
    text: str | None = None
    image: str | None = None  # 画像のパス（ローカル、または s3://…）
    prompt: str = DEFAULT_PROMPT  # テキストを埋め込むときの文の型

    def describe(self) -> str:
        return f"text={self.text!r}" if self.text is not None else f"image={self.image}"


@dataclass(frozen=True)
class SearchParams:
    top_k: int = 10
    min_score: float | None = None  # これより低いスコアの結果は捨てる
    where: str | None = None  # 表の列への条件（SQL の WHERE と同じ書き方）
    level: str = "segments"


def _load_image(uri: str) -> np.ndarray:
    data = read_bytes(*split_uri(uri)) if uri.startswith("s3://") else Path(uri).read_bytes()
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def query_vector(embedder: SiglipEmbedder, query: Query) -> np.ndarray:
    if (query.text is None) == (query.image is None):
        raise ValueError("--text と --image のどちらか 1 つを指定してください")
    if query.text is not None:
        return embedder.embed_texts([query.prompt.format(query.text)])[0]
    return embedder.embed_images([_load_image(query.image)])[0]  # type: ignore[arg-type]


def _nearest(table: Any, vector: np.ndarray, where: str | None, limit: int) -> list[dict[str, Any]]:
    """表の中で vector に近い順に limit 行（コサイン距離 = 1 − コサイン類似度）。where は先に掛ける（prefilter）。"""
    q = table.search(vector, vector_column_name="vector").distance_type("cosine").limit(limit)
    if where:
        q = q.where(where, prefilter=True)
    return q.to_list()


class Searcher:
    """1 つのモデル（表の組）に対する検索。評価（evaluate.py）からも使う。"""

    def __init__(self, vectors_uri: str, embedder: SiglipEmbedder) -> None:
        self.embedder = embedder
        db = connect(vectors_uri)
        self.frames = open_table(db, table_name(embedder.ref, FRAMES))
        self.segments = open_table(db, table_name(embedder.ref, SEGMENTS))
        if self.frames is None or self.segments is None:
            raise FileNotFoundError(f"{vectors_uri} に {table_name(embedder.ref, SEGMENTS)} がありません（先に embedder embed を実行する）")
        self._truth = {r["segment_id"]: r for r in self.segments.to_arrow().select(["segment_id", *TRUTH_COLUMNS]).to_pylist()}

    def _score(self, cosine: float, query: Query) -> float:
        return self.embedder.match_probability(cosine) if query.text is not None else cosine

    def search(self, query: Query, params: SearchParams, vector: np.ndarray | None = None) -> list[dict[str, Any]]:
        if params.level not in LEVELS:
            raise ValueError(f"--level は {LEVELS} のどれか: {params.level}")
        vector = query_vector(self.embedder, query) if vector is None else vector
        if params.level == "segments":
            # しきい値があるときは全件を並べてから切る（しきい値を超える件数が top_k より多い・少ないのどちらもあるため）
            limit = params.top_k if params.min_score is None else self.segments.count_rows()
            hits = [(h["segment_id"], 1.0 - h["_distance"]) for h in _nearest(self.segments, vector, params.where, limit)]
        else:
            # キーフレーム全部と比べ、セグメントごとに一番近いキーフレームを残す（数千行なので全件でよい）
            best: dict[str, float] = {}
            for h in _nearest(self.frames, vector, params.where, self.frames.count_rows()):
                best[h["segment_id"]] = max(best.get(h["segment_id"], -1.0), 1.0 - h["_distance"])
            hits = sorted(best.items(), key=lambda kv: -kv[1])
        results = []
        for seg_id, cosine in hits:
            score = self._score(cosine, query)
            if params.min_score is not None and score < params.min_score:
                continue
            results.append({"rank": len(results) + 1, "segment_id": seg_id, "score": score, "cosine": cosine, **{k: v for k, v in self._truth[seg_id].items() if k != "segment_id"}})
            if len(results) >= params.top_k:
                break
        return results

    def best_keyframe(self, segment_id: str, vector: np.ndarray) -> dict[str, Any]:
        """セグメントの中で、クエリに一番近いキーフレーム（PNG で見せる 1 枚）。"""
        hit = _nearest(self.frames, vector, f"segment_id = '{segment_id}'", 1)[0]
        return {k: hit[k] for k in ("segment_id", "frame_index", "mcap_uri", "chunk_offset", "chunk_length")}


def write_result(results: list[dict[str, Any]], out_uri: str, query: Query, params: SearchParams, embedder: SiglipEmbedder) -> dict[str, Any]:
    """結果を Parquet（segment_id・rank・score の列）で書く。検索の条件は Parquet のメタデータと、横に置く JSON に残す。"""
    meta = {
        "query": asdict(query),
        "params": asdict(params),
        "model": {"name": embedder.ref.name, "version": embedder.ref.version, "hf_repo": embedder.ref.hf_repo, "hf_revision": embedder.ref.hf_revision},
        "results": len(results),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    table = pa.table(
        {
            "segment_id": pa.array([r["segment_id"] for r in results], pa.string()),
            "rank": pa.array([r["rank"] for r in results], pa.int32()),
            "score": pa.array([r["score"] for r in results], pa.float32()),
            "cosine": pa.array([r["cosine"] for r in results], pa.float32()),
        }
    ).replace_schema_metadata({"mini-mlops.search": json.dumps(meta, ensure_ascii=False)})
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    write_bytes(*split_uri(out_uri), buf.getvalue().to_pybytes())
    # 人が読む用の JSON（同じ名前で拡張子だけ .json）
    doc = json.dumps(meta | {"segment_ids": table.column("segment_id").to_pylist()}, ensure_ascii=False, indent=2) + "\n"
    write_bytes(*split_uri(out_uri.removesuffix(".parquet") + ".json"), doc.encode())
    return meta


def save_previews(searcher: Searcher, results: list[dict[str, Any]], vector: np.ndarray, out_dir: Path, count: int) -> list[Path]:
    """上位のセグメントについて、一番近いキーフレームを Range GET でデコードし、正解値と並べて PNG にする。"""
    reader = KeyframeReader()
    paths = []
    try:
        for r in results[:count]:
            frame = searcher.best_keyframe(r["segment_id"], vector)
            image = Image.fromarray(reader.read(frame))
            canvas = Image.new("RGB", (image.width, image.height + 34), (0, 0, 0))
            canvas.paste(image, (0, 0))
            lines = (
                f"#{r['rank']} score={r['score']:.3f} {r['segment_id']} frame={frame['frame_index']}",
                f"truth: {r['time_of_day']}/{r['weather']} small_max={r['small_objects_near_max']} large_ahead={r['large_objects_ahead_any']}",
            )
            for i, line in enumerate(lines):
                ImageDraw.Draw(canvas).text((4, image.height + 4 + 14 * i), line, fill=(255, 255, 255))
            path = out_dir / f"rank-{r['rank']:02d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(path)
            paths.append(path)
    finally:
        reader.close()
    return paths


def format_results(results: list[dict[str, Any]]) -> str:
    lines = ["| 順位 | segment_id | スコア | 時間帯 | 天候 | 小物体（最大） | 進路上の大物体 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r['rank']} | {r['segment_id']} | {r['score']:.3f} | {r['time_of_day']} | {r['weather']} | {r['small_objects_near_max']} | {'あり' if r['large_objects_ahead_any'] else 'なし'} |"
        )
    return "\n".join(lines)
