-- セグメントごとの正解値の集計とタグ付け（indexer tag が実行する）。
-- 入力: frames（フレームごとの正解値）、gops（GOP 索引。索引のあるセッションだけに絞るため）
--
-- 集計の決まり（1 セグメント = 10 秒 = 100 フレーム）:
--   *_max  … 最大値: 「そのセグメントのどこかで、最も多いとき何個写っていたか」（見落としたくない用途向け）
--   *_mean … 平均値: 「どれくらいの時間、写っていたか」の目安（混雑度などの用途向け）
--   *_any  … 1 回でも写ったか（タグはこれで付ける。選別で「含むセグメント」を漏れなく拾うため）
--   time_of_day / weather はセッションの値をそのまま使う（セッション内では変わらない）
SELECT
    f.segment_id,
    any_value(f.session_id) AS session_id,
    any_value(g.device_id) AS device_id,
    min(f.log_time_ns) AS start_time_ns,
    max(f.log_time_ns) AS end_time_ns,
    count(*) AS frames,
    any_value(f.time_of_day) AS time_of_day,
    any_value(f.weather) AS weather,
    max(f.small_objects_near) AS small_objects_near_max,
    avg(f.small_objects_near) AS small_objects_near_mean,
    bool_or(f.small_objects_near > 0) AS small_objects_near_any,
    max(f.large_objects_ahead) AS large_objects_ahead_max,
    avg(f.large_objects_ahead) AS large_objects_ahead_mean,
    bool_or(f.large_objects_ahead > 0) AS large_objects_ahead_any,
    list_filter(
        [
            any_value(f.time_of_day),
            any_value(f.weather),
            CASE WHEN bool_or(f.small_objects_near > 0) THEN 'small_objects' END,
            CASE WHEN bool_or(f.large_objects_ahead > 0) THEN 'large_object_ahead' END
        ],
        x -> x IS NOT NULL
    ) AS tags
FROM frames AS f
JOIN (SELECT DISTINCT session_id, device_id FROM gops) AS g USING (session_id)
GROUP BY f.segment_id
ORDER BY f.segment_id
