-- 手工迁移：先选定目标 schema；应用启动与仓库初始化均不执行此文件。
-- userid 不关联 employees，以支持已认证但不在 SQL 员工表中的开发模拟身份。
CREATE TABLE question_trees (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    name VARCHAR(200) NOT NULL,
    main_leaf_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    INDEX idx_question_trees_owner_updated (owner_userid, updated_at, id),
    CHECK (CHAR_LENGTH(TRIM(name)) > 0),
    CHECK (CHAR_LENGTH(TRIM(owner_userid)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE question_workspaces (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    content JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_question_workspace_tree (tree_id, id),
    FOREIGN KEY (tree_id) REFERENCES question_trees(id),
    CHECK (JSON_TYPE(content) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE question_nodes (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parent_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    kind ENUM('task','summary') NOT NULL,
    status ENUM('generating','complete','cancelled','failed') NOT NULL,
    sequence BIGINT UNSIGNED NOT NULL,
    workspace_version_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_question_node_tree (tree_id, id),
    UNIQUE KEY uq_question_node_sequence (tree_id, sequence),
    INDEX idx_question_node_parent (tree_id, parent_id),
    FOREIGN KEY (tree_id) REFERENCES question_trees(id),
    FOREIGN KEY (tree_id, parent_id) REFERENCES question_nodes(tree_id, id),
    FOREIGN KEY (tree_id, workspace_version_id) REFERENCES question_workspaces(tree_id, id),
    CHECK (sequence > 0),
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK (kind <> 'summary' OR (status = 'complete' AND parent_id IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE question_node_contents (
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    content JSON NOT NULL,
    FOREIGN KEY (node_id) REFERENCES question_nodes(id),
    CHECK (JSON_TYPE(content) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- main_leaf_id 由仓库在持树行锁的事务内校验和更新，避免树/节点建表循环依赖。
