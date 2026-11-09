-- 仅扩展终态，不自动终止任何业务记录；实际处理必须明确指定消息并核对未发布。
ALTER TABLE assistant_robot_inbox
    MODIFY COLUMN status ENUM('received','publish_pending','publish_unknown','publish_blocked','published',
                'reply_pending','reply_unknown','rejected','cancelled') NOT NULL DEFAULT 'received',
    ADD CONSTRAINT chk_robot_inbox_cancellation CHECK (
        status <> 'cancelled' OR (
            publish_started_at IS NULL AND reply_text IS NOT NULL AND CHAR_LENGTH(TRIM(reply_text)) > 0
        )
    );
