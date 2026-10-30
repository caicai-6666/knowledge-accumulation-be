-- 停止所有个人助理写入后显式执行此 DDL，再运行：
-- python -m app.repository.assistant_memory_migration
-- 迁移器按树锁定，保留最新工作区并投影已提交压缩覆盖范围；成功后才清理旧快照。
CREATE TABLE assistant_workspaces (
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    content JSON NOT NULL,
    summary MEDIUMTEXT NULL,
    first_sequence BIGINT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_assistant_workspace_id (id),
    FOREIGN KEY (tree_id) REFERENCES question_trees(id),
    CHECK (JSON_TYPE(content) = 'OBJECT'),
    CHECK (first_sequence > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
