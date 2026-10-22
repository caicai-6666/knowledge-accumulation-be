-- 在 001-question-tree.sql 之后手工执行；本表不保存原文、哈希、模型或维度字段。
CREATE TABLE question_node_memories (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    kind ENUM('question','intermediate','final') NOT NULL,
    embedding MEDIUMBLOB NULL COMMENT '小端连续 float32；模型及维度由统一配置约束',
    embedding_status ENUM('pending','success','failed') NOT NULL DEFAULT 'pending',
    created_at DATETIME(6) NOT NULL,
    embedded_at DATETIME(6) NULL,
    UNIQUE KEY uq_question_memory_source (node_id, kind, source_id),
    INDEX idx_question_memory_status (embedding_status, created_at, id),
    FOREIGN KEY (node_id) REFERENCES question_nodes(id),
    CHECK (CHAR_LENGTH(TRIM(source_id)) > 0),
    CHECK (
        (embedding_status = 'success' AND embedding IS NOT NULL AND embedded_at IS NOT NULL
            AND OCTET_LENGTH(embedding) > 0 AND MOD(OCTET_LENGTH(embedding), 4) = 0)
        OR (embedding_status IN ('pending','failed') AND embedding IS NULL AND embedded_at IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
