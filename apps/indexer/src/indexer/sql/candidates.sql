-- 選別: 外から渡した segment_id の一覧（--segment-ids-uri）をそのまま選ぶ。
-- フェーズ 6 の検索（embedder search）の結果を、学習用のデータセットにするときに使う。
-- candidates は、--segment-ids-uri の Parquet（segment_id 列）を表として見せたもの。
-- segments と結び付けるのは、索引・タグのあるセグメントだけに絞るため（古い検索結果に、消えたセッションが混ざっていても選ばない）
SELECT c.segment_id
FROM candidates AS c
JOIN segments AS s USING (segment_id)
