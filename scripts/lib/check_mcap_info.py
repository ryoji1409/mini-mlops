"""`mcap info` の出力と metadata.json を突き合わせるスクリプト。

使い方:
    mcap --time-format ns info <file.mcap> | python3 -I check_mcap_info.py <metadata.json>

確認すること:
    - トピックの集合が一致する
    - トピックごとのスキーマ名・メッセージ数が一致する
    - 全体のメッセージ数・開始時刻・終了時刻が一致する
一致すれば終了コード 0、どこかが違えば差分を表示して 1 を返す。

Python 標準ライブラリだけで動くようにしている（ホストの python3 でそのまま実行できるように）。
"""

import json
import re
import sys

# `mcap info` のチャネル行の例:
#   \t(1) /imu   \t3000 msgs (100.00..100.03Hz)\t : mini_mlops.Imu [protobuf]
CHANNEL_RE = re.compile(r"^\s*\(\d+\)\s+(?P<topic>\S+)\s+(?P<count>\d+) msgs\b.*:\s+(?P<schema>\S+)\s+\[(?P<encoding>[^\]]+)\]")
# 先頭の要約行の例:  "messages:    3450" / "start:       1793755958000000000"
SUMMARY_RE = re.compile(r"^(?P<key>messages|start|end):\s+(?P<value>\d+)\s*$")


def parse_info(text: str) -> dict:
    """`mcap info` のテキスト出力から、比較に使う値だけを取り出す。"""
    info: dict = {"topics": {}}
    for line in text.splitlines():
        if m := CHANNEL_RE.match(line):
            info["topics"][m["topic"]] = {"count": int(m["count"]), "schema": m["schema"], "encoding": m["encoding"]}
        elif m := SUMMARY_RE.match(line):
            info[m["key"]] = int(m["value"])
    return info


def compare(info: dict, meta: dict) -> list[str]:
    """違っている点を文字列のリストで返す（空なら一致）。"""
    errors: list[str] = []
    meta_topics = {t["topic"]: t for t in meta["topics"]}

    if set(info["topics"]) != set(meta_topics):
        errors.append(f"トピックが一致しません: mcap={sorted(info['topics'])} metadata={sorted(meta_topics)}")

    for topic in sorted(set(info["topics"]) & set(meta_topics)):
        got, want = info["topics"][topic], meta_topics[topic]
        if got["count"] != want["message_count"]:
            errors.append(f"{topic}: メッセージ数 mcap={got['count']} metadata={want['message_count']}")
        if got["schema"] != want["schema"]:
            errors.append(f"{topic}: スキーマ mcap={got['schema']} metadata={want['schema']}")

    total = sum(t["message_count"] for t in meta["topics"])
    checks = [("messages", total), ("start", meta["start_time_ns"]), ("end", meta["end_time_ns"])]
    for key, want in checks:
        if info.get(key) != want:
            errors.append(f"{key}: mcap={info.get(key)} metadata={want}")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        meta = json.load(f)
    info = parse_info(sys.stdin.read())
    errors = compare(info, meta)
    for e in errors:
        print(f"  不一致: {e}", file=sys.stderr)
    if not errors:
        counts = ", ".join(f"{t}={v['count']}" for t, v in sorted(info["topics"].items()))
        print(f"  mcap info と metadata.json が一致（{counts}）", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
