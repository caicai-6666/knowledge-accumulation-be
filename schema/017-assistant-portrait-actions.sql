-- 在 011/016 之后手工执行；序号预留只推进私有计数，不提前创建权威画像。
CREATE TABLE assistant_portrait_sequence (
    id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    next_key BIGINT UNSIGNED NOT NULL,
    CHECK (id=1 AND next_key>0)
) ENGINE=InnoDB;
-- 同时保留表的自增高水位，避免最高条目已被删除后从 MAX(key) 回退并复用旧号。
-- 元数据自增值不能使用统计缓存；只临时调整当前迁移连接，随后恢复原值。
SET @portrait_previous_stats_expiry = @@SESSION.information_schema_stats_expiry;
SET SESSION information_schema_stats_expiry = 0;
INSERT INTO assistant_portrait_sequence(id,next_key)
    SELECT 1,GREATEST(
        (SELECT COALESCE(MAX(`key`),0)+1 FROM assistant_portraits),
        COALESCE((SELECT AUTO_INCREMENT FROM information_schema.TABLES
                  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='assistant_portraits'),1)
    );
SET SESSION information_schema_stats_expiry = @portrait_previous_stats_expiry;

-- 已提交动作的私有去重账本；画像删除后仍保留，禁止重放旧新增而复活画像。
CREATE TABLE assistant_portrait_actions (
    task_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    operation_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (task_id,operation_hash),
    CONSTRAINT fk_portrait_action_task FOREIGN KEY (task_id,owner_userid)
        REFERENCES assistant_task_executions(task_id,userid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
