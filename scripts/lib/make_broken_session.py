"""隔離のテスト用に「壊れたセッション」を作る（Python 標準ライブラリのみ）。

正常なセッションディレクトリの MCAP を途中で切り（後半の summary が無くなる）、
別のセッション ID で metadata.json ごと書き出す。metadata.json の size_bytes / sha256 は
切ったあとのファイルに合わせるので、sha256 の照合は通り、MCAP の構造チェックで落ちる。
（= アップロード前の検証をすり抜けて raw に入ってしまった壊れたファイルを想定）

使い方:
    python3 -I scripts/lib/make_broken_session.py <正常なセッションディレクトリ> <出力ルート>
出力:
    <出力ルート>/device=dev-901/date=2026-11-01/session_id=dev-901-20261101T000000Z/{*.mcap, metadata.json}
    最後の行に、作ったセッションディレクトリの出力ルートからの相対パスを出す
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

DEVICE = "dev-901"
DATE = "2026-11-01"
SESSION_ID = f"{DEVICE}-20261101T000000Z"


def main(src_dir: Path, out_root: Path) -> None:
    meta = json.loads((src_dir / "metadata.json").read_text())
    data = (src_dir / meta["file"]["name"]).read_bytes()
    broken = data[: len(data) // 2]  # 後半（チャンクの続き・summary・footer）を捨てる

    rel = Path(f"device={DEVICE}/date={DATE}/session_id={SESSION_ID}")
    dst = out_root / rel
    dst.mkdir(parents=True, exist_ok=True)
    name = f"{SESSION_ID}.mcap"
    (dst / name).write_bytes(broken)

    meta.update(device_id=DEVICE, session_id=SESSION_ID)
    meta["file"].update(name=name, size_bytes=len(broken), sha256=hashlib.sha256(broken).hexdigest())
    meta["note"] = "隔離のテスト用に意図的に壊したファイル（docs/setup/02-pipeline.md 手順 10）"
    (dst / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(rel)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
