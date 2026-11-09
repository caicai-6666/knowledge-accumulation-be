-- 文件交付复用可靠发送账本；旧记录缺省 NULL，正文与每份文件分别使用稳定动作键。
ALTER TABLE assistant_deliveries
    ADD COLUMN file_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD CONSTRAINT chk_assistant_delivery_file CHECK (
        file_id IS NULL OR (destination='user' AND task_id IS NOT NULL AND recipient_userid=userid
        AND REGEXP_LIKE(file_id,'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$','c')));
