-- 节点落盘记录；在 001、002 之后手工执行，不依赖 MyVector 缓存。
CREATE TABLE question_node_messages (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    kind ENUM('question','intermediate','final') NOT NULL,
    text LONGTEXT NOT NULL,
    sequence BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_question_message_sequence (node_id,sequence),
    INDEX idx_question_message_source (node_id,kind,id),
    FOREIGN KEY (node_id) REFERENCES question_nodes(id),
    CHECK (sequence > 0),
    CHECK (CHAR_LENGTH(TRIM(text)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE question_node_generations (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status ENUM('complete','failed','cancelled') NOT NULL,
    payload JSON NOT NULL COMMENT '完整原生响应、推理与流式片段，仅供私有恢复和审计',
    sequence BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_question_generation_sequence (node_id,sequence),
    FOREIGN KEY (node_id) REFERENCES question_nodes(id),
    CHECK (sequence > 0),
    CHECK (JSON_TYPE(payload) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
