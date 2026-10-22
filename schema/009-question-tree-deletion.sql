-- 手工迁移；保存数据库删除提交后仍需清理的附件，不依赖已删除的树外键。
CREATE TABLE question_tree_deletions (
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    attachments JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CHECK (JSON_TYPE(attachments) = 'ARRAY')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
