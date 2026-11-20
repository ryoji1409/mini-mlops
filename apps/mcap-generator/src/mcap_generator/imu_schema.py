"""IMU 用の独自 protobuf スキーマ `mini_mlops.Imu` を定義するモジュール。

Foxglove の公式スキーマには IMU 型がないため、ROS の sensor_msgs/Imu と同じ形のメッセージを自作する。
中身の部品（Quaternion / Vector3）は Foxglove の型を使うので、Foxglove の Plot パネル等でそのまま見られる。

通常は .proto ファイルを書いて protoc でコードを生成するが、
そのためだけにツール（protoc / grpcio-tools）を増やしたくないので、
Python 上でスキーマ定義（FileDescriptorProto）を組み立て、実行時にメッセージクラスを作る。

作られるスキーマを .proto で書くと次と同じ意味になる:

    syntax = "proto3";
    package mini_mlops;
    import "google/protobuf/timestamp.proto";
    import "foxglove/Quaternion.proto";
    import "foxglove/Vector3.proto";

    message Imu {
      google.protobuf.Timestamp timestamp = 1;       // 計測時刻
      string frame_id = 2;                           // 座標系の名前
      foxglove.Quaternion orientation = 3;           // 姿勢
      foxglove.Vector3 angular_velocity = 4;         // 角速度 [rad/s]
      foxglove.Vector3 linear_acceleration = 5;      // 加速度 [m/s^2]
    }
"""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf import timestamp_pb2

# 依存スキーマを import しておくと、それらが「デフォルトのスキーマ置き場（pool）」に登録される。
# Imu はこれらを参照するので、先に登録されている必要がある。
from foxglove_schemas_protobuf import Quaternion_pb2, Vector3_pb2

IMU_FULL_NAME = "mini_mlops.Imu"
_FILE_NAME = "mini_mlops/Imu.proto"

_F = descriptor_pb2.FieldDescriptorProto


def _build_file_descriptor() -> descriptor_pb2.FileDescriptorProto:
    """上のコメントにある .proto と同じ内容の定義を組み立てる。"""
    fd = descriptor_pb2.FileDescriptorProto(
        name=_FILE_NAME,
        package="mini_mlops",
        syntax="proto3",
        dependency=[
            timestamp_pb2.DESCRIPTOR.name,
            Quaternion_pb2.DESCRIPTOR.name,
            Vector3_pb2.DESCRIPTOR.name,
        ],
    )
    msg = fd.message_type.add(name="Imu")
    # (フィールド名, 番号, 型, 型名) の一覧。型名はメッセージ型のときだけ使う
    fields = [
        ("timestamp", 1, _F.TYPE_MESSAGE, ".google.protobuf.Timestamp"),
        ("frame_id", 2, _F.TYPE_STRING, None),
        ("orientation", 3, _F.TYPE_MESSAGE, ".foxglove.Quaternion"),
        ("angular_velocity", 4, _F.TYPE_MESSAGE, ".foxglove.Vector3"),
        ("linear_acceleration", 5, _F.TYPE_MESSAGE, ".foxglove.Vector3"),
    ]
    for name, number, ftype, type_name in fields:
        field = msg.field.add(name=name, number=number, type=ftype, label=_F.LABEL_OPTIONAL)
        if type_name:
            field.type_name = type_name
    return fd


def _get_imu_class() -> type:
    """スキーマを pool に登録し、メッセージクラスを返す（2 回目以降は登録済みのものを使う）。"""
    pool = descriptor_pool.Default()
    try:
        descriptor = pool.FindMessageTypeByName(IMU_FULL_NAME)
    except KeyError:
        pool.Add(_build_file_descriptor())
        descriptor = pool.FindMessageTypeByName(IMU_FULL_NAME)
    return message_factory.GetMessageClass(descriptor)


# 他のモジュールからは `from .imu_schema import Imu` として普通のクラスのように使える
Imu = _get_imu_class()
