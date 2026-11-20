"""IMU と GNSS の値を作るモジュール。

まず「デバイスがどう動いたか」（軌跡）を 100Hz で計算し、そこから
- IMU（100Hz）: 加速度・角速度・姿勢
- GNSS（10Hz）: 緯度経度
を取り出す。同じ軌跡から両方を作るので、IMU と GNSS の値が物理的に矛盾しない。
"""

import math
from dataclasses import dataclass

import numpy as np

# 軌跡を計算する刻み（IMU と同じ 100Hz）
BASE_RATE_HZ = 100
DT_S = 1.0 / BASE_RATE_HZ

GRAVITY_MPS2 = 9.80665
EARTH_RADIUS_M = 6_378_137.0

# 架空のコースの原点（緯度経度）。実在の場所とは関係ない。
# デバイスごとに少しずらして、別々の場所を動いているように見せる。
ORIGIN_LAT_DEG = 35.0
ORIGIN_LON_DEG = 139.0
DEVICE_ORIGIN_OFFSET_DEG = 0.02


@dataclass(frozen=True)
class Trajectory:
    """100Hz で並んだデバイスの状態。どの配列も長さ = 記録秒数 × 100。"""

    speed_mps: np.ndarray  # 速度 [m/s]
    accel_mps2: np.ndarray  # 前後方向の加速度 [m/s^2]
    yaw_rad: np.ndarray  # 進行方向（東=0, 反時計回り）[rad]
    yaw_rate_rps: np.ndarray  # 旋回の角速度 [rad/s]
    lat_deg: np.ndarray  # 緯度
    lon_deg: np.ndarray  # 経度

    def __len__(self) -> int:
        return len(self.speed_mps)


def device_origin(device_index: int) -> tuple[float, float]:
    """デバイス番号ごとのコース原点（緯度, 経度）を返す。"""
    return (
        ORIGIN_LAT_DEG + DEVICE_ORIGIN_OFFSET_DEG * device_index,
        ORIGIN_LON_DEG + DEVICE_ORIGIN_OFFSET_DEG * device_index,
    )


def make_trajectory(
    rng: np.random.Generator,
    duration_s: int,
    base_speed_mps: float,
    origin: tuple[float, float],
) -> Trajectory:
    """ゆるやかに速度と向きが変化する軌跡を作る。

    - 速度: 基準速度のまわりを正弦波でゆっくり上下させる
    - 旋回: 角速度を正弦波で左右に振る（S 字カーブが続くイメージ）
    それを積分して進行方向 → 位置（メートル）→ 緯度経度、と求める。
    """
    n = duration_s * BASE_RATE_HZ
    t = np.arange(n) * DT_S

    # 速度: 振幅 0.5m/s、周期 15〜25 秒で増減させる
    speed_period = rng.uniform(15.0, 25.0)
    speed = base_speed_mps + 0.5 * np.sin(2 * math.pi * t / speed_period)
    accel = np.gradient(speed, DT_S)  # 速度の時間微分 = 加速度

    # 旋回: 最大 0.05〜0.15 rad/s（3〜9 度/秒）程度のゆるいカーブ
    turn_amp = rng.uniform(0.05, 0.15)
    turn_period = rng.uniform(20.0, 40.0)
    turn_phase = rng.uniform(0, 2 * math.pi)
    yaw_rate = turn_amp * np.sin(2 * math.pi * t / turn_period + turn_phase)
    yaw = rng.uniform(-math.pi, math.pi) + np.cumsum(yaw_rate) * DT_S

    # 位置（原点からの東向き x / 北向き y [m]）を積分で求める
    x = np.cumsum(speed * np.cos(yaw)) * DT_S
    y = np.cumsum(speed * np.sin(yaw)) * DT_S

    # メートル → 緯度経度（狭い範囲なので平面近似で十分）
    lat0, lon0 = origin
    lat = lat0 + np.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + np.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))

    return Trajectory(speed, accel, yaw, yaw_rate, lat, lon)


@dataclass(frozen=True)
class ImuSample:
    """1 時刻分の IMU の値（機体座標系: x=前, y=左, z=上）。"""

    orientation_xyzw: tuple[float, float, float, float]
    angular_velocity: tuple[float, float, float]
    linear_acceleration: tuple[float, float, float]


def imu_sample(traj: Trajectory, i: int, rng: np.random.Generator) -> ImuSample:
    """軌跡の i 番目から IMU の値を作る。実センサーっぽくノイズを足す。"""
    yaw = float(traj.yaw_rad[i])
    # ロール・ピッチは 0 とみなし、ヨー（z 軸回り）だけの回転をクォータニオンで表す
    orientation = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))

    gyro_noise = rng.normal(0.0, 0.002, size=3)
    angular_velocity = (
        float(gyro_noise[0]),
        float(gyro_noise[1]),
        float(traj.yaw_rate_rps[i] + gyro_noise[2]),
    )

    acc_noise = rng.normal(0.0, 0.05, size=3)
    # 横方向の加速度 = 速度 × 角速度（旋回で外側に押される力）
    lateral = traj.speed_mps[i] * traj.yaw_rate_rps[i]
    linear_acceleration = (
        float(traj.accel_mps2[i] + acc_noise[0]),
        float(lateral + acc_noise[1]),
        float(GRAVITY_MPS2 + acc_noise[2]),  # 静止していても重力で上向き 1G が出る
    )
    return ImuSample(orientation, angular_velocity, linear_acceleration)


@dataclass(frozen=True)
class GnssSample:
    latitude: float
    longitude: float
    altitude: float
    sigma_m: float  # 水平方向の位置誤差の標準偏差 [m]


def gnss_sample(traj: Trajectory, i: int, rng: np.random.Generator) -> GnssSample:
    """軌跡の i 番目から GNSS の値を作る。位置に 0.5m 程度の誤差を足す。"""
    sigma_m = 0.5
    north_err, east_err = rng.normal(0.0, sigma_m, size=2)
    lat = float(traj.lat_deg[i]) + math.degrees(north_err / EARTH_RADIUS_M)
    lon = float(traj.lon_deg[i]) + math.degrees(
        east_err / (EARTH_RADIUS_M * math.cos(math.radians(float(traj.lat_deg[i]))))
    )
    altitude = 40.0 + float(rng.normal(0.0, 1.0))
    return GnssSample(lat, lon, altitude, sigma_m)
