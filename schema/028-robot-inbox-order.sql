-- 维护窗口执行：先暂停机器人接收/发布，完成迁移后再启动新版服务。
-- 旧账本按接收时间回填，新记录由数据库递增分配；重推保留原位置。
ALTER TABLE assistant_robot_inbox
    ADD COLUMN arrival_sequence BIGINT UNSIGNED NULL,
    ADD UNIQUE KEY uq_robot_inbox_arrival (arrival_sequence),
    ADD INDEX idx_robot_inbox_user_arrival (userid,arrival_sequence,status);

UPDATE assistant_robot_inbox AS target
JOIN (SELECT request_id, ROW_NUMBER() OVER (ORDER BY created_at,request_id) AS position
      FROM assistant_robot_inbox) AS ordered ON ordered.request_id=target.request_id
SET target.arrival_sequence=ordered.position;

ALTER TABLE assistant_robot_inbox
    MODIFY COLUMN arrival_sequence BIGINT UNSIGNED NOT NULL AUTO_INCREMENT;
