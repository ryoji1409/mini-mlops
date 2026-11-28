-- 選別の例: 検索結果（candidates）のうち、正解値のタグでも「雨」のセグメントだけを選ぶ。
-- 検索（似ているかどうか）とタグ（正解値）の条件を組み合わせる例
SELECT c.segment_id
FROM candidates AS c
JOIN segments AS s USING (segment_id)
WHERE s.weather = 'rain'
