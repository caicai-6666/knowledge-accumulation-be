-- 在 001、004 之后手工执行；保留现有状态名，superseded 表示被用户新要求替代。
-- 旧记录缺少结束时间时保持 NULL，不用创建时间伪造执行时长。
ALTER TABLE question_nodes
    MODIFY COLUMN status ENUM('generating','complete','cancelled','superseded','failed') NOT NULL,
    ADD COLUMN finished_at DATETIME(6) NULL,
    ADD COLUMN superseded_by_node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD UNIQUE KEY uq_question_node_replacement (tree_id,superseded_by_node_id),
    ADD FOREIGN KEY (tree_id,superseded_by_node_id) REFERENCES question_nodes(tree_id,id),
    ADD CHECK ((status='superseded')=(superseded_by_node_id IS NOT NULL)),
    ADD CHECK (superseded_by_node_id IS NULL OR superseded_by_node_id<>id),
    ADD CHECK (finished_at IS NULL OR (status<>'generating' AND finished_at>=created_at));

-- 只归档完整消息或中断正文，流式 delta 不直接写入消息表。
ALTER TABLE question_node_messages
    ADD COLUMN status ENUM('completed','interrupted') NOT NULL DEFAULT 'completed' AFTER kind,
    ADD CHECK (kind<>'question' OR status='completed');
