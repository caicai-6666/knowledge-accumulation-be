-- 在本项目使用的 MySQL 库手工执行一次；应用不自动迁移。
-- id 直接使用已登记附件的 UUID，不另建文件表或数据库外键。
-- 本迁移仅建空表；文件存在性、所属用户、上传时间来源及共享权限由后续业务接入校验。
CREATE TABLE assistant_file_notes (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '已登记附件的完整小写 UUID，同时作为备忘唯一标识',
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL COMMENT '附件所属用户，由可信身份及文件登记确定',
    name VARCHAR(200) NOT NULL COMMENT '模型拟定或与用户协商的文件名称',
    summary TEXT NOT NULL COMMENT '文件摘要，用于后续定位与查找',
    visibility VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'public' COMMENT '可见范围：public 公开，private 私密',
    updated_at DATETIME(6) NOT NULL COMMENT '备忘最近更新时间，UTC，由程序维护',
    uploaded_at DATETIME(6) NOT NULL COMMENT '文件上传时间，UTC，从可信来源取得，不能用创建备忘时间替代',
    PRIMARY KEY (id),
    KEY idx_assistant_file_notes_owner_updated (owner_userid, updated_at, id),
    CONSTRAINT chk_assistant_file_notes_id CHECK (
        REGEXP_LIKE(id, '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', 'c')),
    CONSTRAINT chk_assistant_file_notes_owner CHECK (REGEXP_LIKE(owner_userid, '[^[:space:]]')),
    CONSTRAINT chk_assistant_file_notes_name CHECK (REGEXP_LIKE(name, '[^[:space:]]')),
    CONSTRAINT chk_assistant_file_notes_summary CHECK (REGEXP_LIKE(summary, '[^[:space:]]')),
    CONSTRAINT chk_assistant_file_notes_visibility CHECK (CAST(visibility AS BINARY) IN ('public', 'private')),
    CONSTRAINT chk_assistant_file_notes_time CHECK (updated_at >= uploaded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='个人助理附件备忘，一份文件最多一条记录';
