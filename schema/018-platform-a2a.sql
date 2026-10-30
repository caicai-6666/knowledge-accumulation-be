-- 跨平台工具的私有请求/反馈账本；手工迁移，应用不创建表。
-- 不保存平台 token、反馈 delivery_token 或模型私有推理。
CREATE TABLE platform_a2a_calls (
    broadcast_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_json JSON NOT NULL,
    targets JSON NOT NULL,
    published JSON NULL,
    replies JSON NOT NULL,
    result_json JSON NULL,
    state ENUM('pending','waiting','completed','abandoned') NOT NULL DEFAULT 'pending',
    accepted BOOLEAN NOT NULL DEFAULT FALSE,
    close_pending BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME(6) NOT NULL,
    deadline_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    INDEX idx_platform_a2a_pending(state,deadline_at),
    INDEX idx_platform_a2a_context(userid,tree_id,accepted),
    -- 私有收发账本保留到业务树删除后，用于关闭广播与确认迟到反馈，不阻塞树删除。
    CHECK (JSON_TYPE(request_json)='OBJECT'),
    CHECK (JSON_TYPE(targets)='ARRAY'),
    CHECK (JSON_TYPE(replies)='OBJECT'),
    CHECK (accepted IN (0,1) AND close_pending IN (0,1)),
    CHECK (deadline_at>created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
