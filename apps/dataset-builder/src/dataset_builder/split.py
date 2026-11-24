"""学習用 / 検証用の分割。**セッション単位** で分ける。

同じセッションの連続したフレームはほとんど同じ見た目なので、フレーム単位でランダムに分けると、
「学習で見た画像とほぼ同じ画像」で答え合わせをすることになり、精度が不当に高く出る。
そこで、セッションごとに「全部学習用」か「全部検証用」のどちらかにする。

さらに、検証用に全クラスが入るよう、（時間帯, 天候）の組ごとに決まった数のセッションを検証用に回す（層化）。
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict

from dataset_builder.sources import SessionSource

log = logging.getLogger(__name__)


def split_sessions(sources: list[SessionSource], val_per_combo: int, seed: int) -> dict[str, str]:
    """session_id → "train" / "val" を返す。同じ入力・同じ seed なら必ず同じ分割になる。

    - 組ごとにセッションを session_id 順に並べてから、seed で並べ替え、先頭 val_per_combo 本を検証用にする
    - 組のセッションが val_per_combo 本以下しかないときは、少なくとも 1 本は学習用に残す
    """
    by_combo: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in sources:
        by_combo[s.combo].append(s.session_id)

    assignment: dict[str, str] = {}
    for combo in sorted(by_combo):
        ids = sorted(by_combo[combo])
        # 組ごとに別の乱数列にする（組の並び順や他の組の本数に左右されないように）
        random.Random(f"{seed}:{combo[0]}:{combo[1]}").shuffle(ids)
        n_val = min(val_per_combo, max(0, len(ids) - 1))
        if n_val < val_per_combo:
            log.warning("組 %s はセッションが %d 本しかないので、検証用は %d 本にします", combo, len(ids), n_val)
        for i, session_id in enumerate(ids):
            assignment[session_id] = "val" if i < n_val else "train"
    return assignment
