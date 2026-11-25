-- 選別の例: 夕方で、近くに小物体が 2 個以上写る時間があったセグメント（タグと集計値の組み合わせ）
SELECT segment_id
FROM segments
WHERE time_of_day = 'dusk' AND small_objects_near_max >= 2
