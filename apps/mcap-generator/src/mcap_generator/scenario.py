"""セッション（session）ごとの条件を決めるモジュール。

時間帯・天候・シーン内の物体の数などを、乱数シードから「決定的に」決める。
同じシードなら何度実行しても同じ条件になるので、生成結果（sha256）も同じになる。
→ アップロードの冪等性（同じファイルなら二重に上げない）をテストしやすくなる。
"""

import itertools
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .sensors import BASE_RATE_HZ

# 時間帯と天候の選択肢。metadata.json に「正解ラベル」としてそのまま書く値。
TIME_OF_DAY_CHOICES: tuple[str, ...] = ("day", "dusk", "night")
WEATHER_CHOICES: tuple[str, ...] = ("clear", "rain")

# 記録は日本国内を想定し、「時間帯」は日本時間（JST）で決める。
# ただし MCAP のタイムスタンプやパーティションの date は UTC で扱う（タイムゾーンの混乱を避けるため）。
JST = timezone(timedelta(hours=9))

# 時間帯ごとに、記録開始時刻（JST の「時」）として取りうる範囲 [開始, 終了]
LOCAL_HOUR_RANGES: dict[str, tuple[int, int]] = {
    "day": (9, 15),
    "dusk": (16, 17),
    "night": (19, 23),
}

# 記録日をばらけさせる日数の幅（start_date から何日後までを使うか）
DATE_SPREAD_DAYS = 14


@dataclass(frozen=True)
class SessionScenario:
    """1 セッション分の条件。frozen=True なので作成後に値を書き換えられない（うっかり変更を防ぐ）。"""

    device_id: str
    session_id: str
    start_time: datetime  # UTC（タイムゾーン付き）
    duration_s: int
    time_of_day: str  # "day" / "dusk" / "night"
    weather: str  # "clear" / "rain"
    num_large_objects: int  # 大きな物体（箱）の数
    num_small_objects: int  # 小さな物体（柱）の数
    base_speed_mps: float  # 基準の移動速度 [m/s]
    seed: int  # このセッション専用の乱数シード（描画・センサー値に使う）

    @property
    def start_time_ns(self) -> int:
        """開始時刻を UNIX エポックからのナノ秒で返す（MCAP のタイムスタンプ形式）。"""
        return int(self.start_time.timestamp()) * 1_000_000_000

    @property
    def end_time_ns(self) -> int:
        """最後のメッセージの時刻 [ns]。

        メッセージは 100Hz の刻みで「開始時刻 + 0, 10ms, 20ms, ...」に置くので、
        最後は「開始 + 記録秒数 − 10ms」になる（mcap info の end と一致させるため）。
        """
        tick_ns = 1_000_000_000 // BASE_RATE_HZ
        return self.start_time_ns + self.duration_s * 1_000_000_000 - tick_ns


def device_id_of(index: int) -> str:
    """0 始まりの番号からデバイス ID を作る（例: 0 → "dev-001"）。"""
    return f"dev-{index + 1:03d}"


def make_session_id(device_id: str, start_time: datetime) -> str:
    """デバイス ID と開始時刻（UTC）から、全体で一意になるセッション ID を作る。"""
    return f"{device_id}-{start_time:%Y%m%dT%H%M%SZ}"


def _condition_cycle(rng: random.Random) -> itertools.chain[tuple[str, str]]:
    """（時間帯, 天候）の組み合わせを、偏りなく順番に返すイテレータを作る。

    完全にランダムだと、セッション数が少ないとき「全部 昼・晴れ」のように偏ることがある。
    後のフェーズの画像検索で違いを確認したいので、
    6 通りの組み合わせをシャッフルしたものを 1 周ずつ使う。
    """
    combos = list(itertools.product(TIME_OF_DAY_CHOICES, WEATHER_CHOICES))

    def rounds():
        while True:
            shuffled = combos[:]
            rng.shuffle(shuffled)
            yield shuffled

    return itertools.chain.from_iterable(rounds())


def _pick_start_time(rng: random.Random, start_date: date, time_of_day: str) -> datetime:
    """時間帯に合う開始時刻（JST で決めて UTC に変換）を乱数で選ぶ。"""
    day = start_date + timedelta(days=rng.randrange(DATE_SPREAD_DAYS))
    hour_min, hour_max = LOCAL_HOUR_RANGES[time_of_day]
    local = datetime.combine(
        day,
        time(hour=rng.randint(hour_min, hour_max), minute=rng.randrange(60), second=rng.randrange(60)),
        tzinfo=JST,
    )
    return local.astimezone(timezone.utc)


def make_scenarios(
    num_devices: int,
    sessions_per_device: int,
    seed: int,
    min_duration_s: int,
    max_duration_s: int,
    start_date: date,
    first_device: int = 1,
) -> list[SessionScenario]:
    """全デバイス・全セッションの条件をまとめて作る。

    first_device はデバイス番号の始まり（1 なら dev-001 から）。
    既にアップロード済みのデータと ID が重ならないデータを追加で作るときに、番号をずらして使う
    （例: first_device=11 → dev-011, dev-012, …）。番号をずらしても乱数の使い方は変わらない。
    """
    if not 1 <= min_duration_s <= max_duration_s:
        raise ValueError("duration の指定が不正です（1 <= min <= max）")
    if first_device < 1:
        raise ValueError("first_device は 1 以上を指定してください")

    rng = random.Random(seed)
    conditions = _condition_cycle(rng)
    scenarios: list[SessionScenario] = []
    used_session_ids: set[str] = set()

    for v in range(num_devices):
        device_id = device_id_of(first_device - 1 + v)
        for _ in range(sessions_per_device):
            time_of_day, weather = next(conditions)
            # セッション ID が衝突しないよう、万一重なったら時刻を選び直す
            while True:
                start_time = _pick_start_time(rng, start_date, time_of_day)
                session_id = make_session_id(device_id, start_time)
                if session_id not in used_session_ids:
                    used_session_ids.add(session_id)
                    break
            scenarios.append(
                SessionScenario(
                    device_id=device_id,
                    session_id=session_id,
                    start_time=start_time,
                    duration_s=rng.randint(min_duration_s, max_duration_s),
                    time_of_day=time_of_day,
                    weather=weather,
                    num_large_objects=rng.randint(0, 3),
                    num_small_objects=rng.randint(0, 3),
                    base_speed_mps=round(rng.uniform(1.0, 3.0), 2),
                    seed=rng.randrange(2**31),
                )
            )
    return scenarios
