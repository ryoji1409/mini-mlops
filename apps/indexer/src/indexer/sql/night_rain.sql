-- 選別: 夜かつ雨のセグメント（完了条件の確認用）。
-- 選別の SQL は、segment_id の列を返せばよい（他の列は使わない）。使える表: segments、gops、frames
SELECT segment_id
FROM segments
WHERE time_of_day = 'night' AND weather = 'rain'
