-- 手工在知识后端助理库执行；不由启动创建表。不保存下载码、访问令牌或文件二进制。
CREATE TABLE assistant_robot_inbox (
    request_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    message TEXT NOT NULL,
    files JSON NOT NULL,
    status ENUM('received','publish_pending','publish_unknown','publish_blocked','published',
                'reply_pending','reply_unknown','rejected') NOT NULL DEFAULT 'received',
    reply_text VARCHAR(1000) NULL,
    publish_started_at DATETIME(6) NULL,
    next_attempt_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    INDEX idx_robot_inbox_retry(status,next_attempt_at,request_id),
    CHECK(JSON_TYPE(files)='ARRAY'),
    CHECK(CHAR_LENGTH(message) BETWEEN 1 AND 10000)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
