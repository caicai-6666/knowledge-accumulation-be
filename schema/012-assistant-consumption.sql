-- 在 011 之后手工迁移。允许已完成但尚未收到 ack 回执的任务等待重领，不阻塞新高优先级任务。
ALTER TABLE assistant_task_executions
    DROP INDEX uq_assistant_unacknowledged,
    ADD UNIQUE KEY uq_assistant_active ((CASE WHEN processing_status IN ('received','prepared','running') THEN userid ELSE NULL END)),
    ADD COLUMN delivery_token VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '仅用于恢复 ack，不进入业务输入/模型上下文/日志',
    ADD COLUMN lease_expires_at DATETIME(6) NULL,
    ADD CONSTRAINT chk_assistant_lease_pair CHECK ((delivery_token IS NULL)=(lease_expires_at IS NULL));
