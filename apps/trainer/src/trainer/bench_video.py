"""動画ローダーの計測（bench-video）。

manifest のサンプルを 1 エポック分読み、
- 1 秒あたりのサンプル数（samples/sec）
- S3 から読んだバイト数（Range GET の合計）と回数
を、「人工的な遅延（latency）× 先読み（prefetch）」の組み合わせごとに測って表にする。
読んだバイト数は、選ばれたセッションの MCAP の合計・raw ゾーンの全 MCAP の合計と並べて示す。
また、目で確かめるために、最初の数サンプルの「カメラ画像 + 経路ラスタ」を PNG に保存する。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from indexer.rangefile import ReadStats
from PIL import Image, ImageDraw

from trainer.storage import list_files, open_location
from trainer.video_dataset import VideoManifestDataset

log = logging.getLogger(__name__)

PROGRESS_EVERY = 50  # 途中経過を出す間隔（サンプル数）


@dataclass(frozen=True)
class BenchResult:
    latency_ms: float
    prefetch: int
    samples: int
    seconds: float
    samples_per_sec: float
    bytes_read: int
    requests: int


def raw_mcap_total(raw_uri: str) -> tuple[int, int]:
    """raw ゾーンの全 MCAP のサイズの合計と個数（ファイルの情報を一覧するだけで、中身は読まない）。"""
    loc = open_location(raw_uri)
    rels = list_files(loc, "", suffix=".mcap")
    infos = loc.fs.get_file_info([loc.path(r) for r in rels])
    return sum(i.size for i in infos), len(infos)


def save_preview(sample: dict[str, Any], path: Path) -> None:
    """カメラ画像と経路ラスタを横に並べ、正解値を書き込んだ PNG を保存する。"""
    cam = Image.fromarray(sample["image"].numpy().transpose(1, 2, 0))
    raster = Image.fromarray(sample["route"].numpy()[0]).convert("RGB").resize((cam.height, cam.height), Image.Resampling.NEAREST)
    canvas = Image.new("RGB", (cam.width + raster.width + 4, cam.height + 30), (32, 32, 32))
    canvas.paste(cam, (0, 0))
    canvas.paste(raster, (cam.width + 4, 0))
    text = (
        f"{sample['segment_id']} frame={sample['frame_index']} {sample['time_of_day']}/{sample['weather']} "
        f"small={sample['small_objects_near']} large_ahead={sample['large_objects_ahead']} heading={sample['heading_source']}"
    )
    ImageDraw.Draw(canvas).text((4, cam.height + 8), text, fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)


def run_once(manifest_uri: str, latency_ms: float, prefetch: int, decoder: str, preview_dir: Path | None, preview_count: int) -> BenchResult:
    stats = ReadStats()
    ds = VideoManifestDataset(manifest_uri, prefetch=prefetch, latency_ms=latency_ms, decoder=decoder, stats=stats)
    start = time.perf_counter()
    n = 0
    for sample in ds:
        if preview_dir is not None and n < preview_count:
            save_preview(sample, preview_dir / f"sample-{n:02d}.png")
        n += 1
        if n % PROGRESS_EVERY == 0:  # S3 が遅いと 1 回の計測に数分かかるので、止まっていないことが分かるように途中経過を出す
            bytes_read, requests = stats.snapshot()
            log.info("  … %d サンプル / %.1f 秒、読んだバイト数 %d（%d 回）", n, time.perf_counter() - start, bytes_read, requests)
    seconds = time.perf_counter() - start
    bytes_read, requests = stats.snapshot()
    return BenchResult(latency_ms, prefetch, n, round(seconds, 2), round(n / seconds, 1), bytes_read, requests)


def bench(
    manifest_uri: str, raw_uri: str, latencies: list[float], prefetches: list[int], decoder: str, preview_dir: Path | None, preview_count: int
) -> dict[str, Any]:
    manifest = VideoManifestDataset(manifest_uri).manifest
    results = []
    for latency in latencies:
        for prefetch in prefetches:
            first = not results
            r = run_once(manifest_uri, latency, prefetch, decoder, preview_dir if first else None, preview_count)
            log.info(
                "latency=%3.0fms prefetch=%d: %d サンプル / %.1f 秒 = %.1f samples/sec、読んだバイト数 %d（%d 回）",
                r.latency_ms, r.prefetch, r.samples, r.seconds, r.samples_per_sec, r.bytes_read, r.requests,
            )
            results.append(r)

    raw_total, raw_files = raw_mcap_total(raw_uri)
    selected = manifest["selected_mcap_bytes"]
    read = results[0].bytes_read
    log.info("読んだバイト数 %d = 選ばれたセッションの MCAP の合計 %d（%d ファイル）の %.1f%% / raw の全 MCAP %d（%d ファイル）の %.2f%%",
             read, selected, manifest["counts"]["mcap_files"], 100 * read / selected, raw_total, raw_files, 100 * read / raw_total)
    log.info("（参考）選ばれた GOP のチャンクの合計 %d。差分は summary・予定ルート・manifest のシャードを読んだ分", manifest["selected_chunk_bytes"])
    return {
        "manifest": manifest_uri,
        "samples": results[0].samples,
        "frames": manifest["frames"],
        "selected_chunk_bytes": manifest["selected_chunk_bytes"],
        "selected_mcap_bytes": selected,
        "raw_mcap_bytes": raw_total,
        "raw_mcap_files": raw_files,
        "results": [asdict(r) for r in results],
    }


def format_table(summary: dict[str, Any]) -> str:
    lines = ["| latency | prefetch | samples/sec | 秒 | 読んだバイト数 | Range GET の回数 |", "|---|---|---|---|---|---|"]
    for r in summary["results"]:
        lines.append(f"| {r['latency_ms']:.0f}ms | {r['prefetch']} | {r['samples_per_sec']} | {r['seconds']} | {r['bytes_read']:,} | {r['requests']} |")
    return "\n".join(lines)


