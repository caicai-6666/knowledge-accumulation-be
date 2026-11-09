-- 在 023 之后手工执行一次；应用不自动迁移。
-- 执行前确认 assistant_file_notes 为空且暂无写入方。旧记录的 owner_userid
-- 无法区分上传者和记录者；若已有记录，必须先核实真实元信息并另行制定迁移方案。
-- 不从旧备忘推断上传者、文件类型或权限，也不扫描磁盘伪造上传时间。
CREATE TABLE uploaded_files (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '现有附件的完整小写 UUID',
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL COMMENT '文件上传者，由可信上传身份确定',
    type VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '附件类型：images、audio 或 video，与附件引用一致',
    visibility VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'public' COMMENT '文件可见范围：public 公开，private 私密，仅上传者可修改',
    uploaded_at DATETIME(6) NOT NULL COMMENT '真实上传时间，UTC，由可信上传流程提供',
    updated_at DATETIME(6) NOT NULL COMMENT '文件元信息最近更新时间，UTC，由程序维护',
    PRIMARY KEY (id),
    KEY idx_uploaded_files_owner_uploaded (owner_userid, uploaded_at, id),
    CONSTRAINT chk_uploaded_files_id CHECK (
        REGEXP_LIKE(id, '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', 'c')),
    CONSTRAINT chk_uploaded_files_owner CHECK (REGEXP_LIKE(owner_userid, '[^[:space:]]')),
    CONSTRAINT chk_uploaded_files_type CHECK (CAST(type AS BINARY) IN ('images', 'audio', 'video')),
    CONSTRAINT chk_uploaded_files_visibility CHECK (CAST(visibility AS BINARY) IN ('public', 'private')),
    CONSTRAINT chk_uploaded_files_time CHECK (updated_at >= uploaded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户上传文件元信息，与个人备忘独立';

-- 同一文件允许不同用户各自记录；删除备忘不会删除文件或其他用户的记录。
-- 外键限制删除仍被引用的文件元信息，后续文件删除服务须显式处理关联备忘。
ALTER TABLE assistant_file_notes
    DROP CHECK chk_assistant_file_notes_id,
    DROP CHECK chk_assistant_file_notes_visibility,
    DROP CHECK chk_assistant_file_notes_time,
    DROP PRIMARY KEY,
    DROP INDEX idx_assistant_file_notes_owner_updated,
    CHANGE COLUMN id file_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联 uploaded_files.id 的附件 UUID',
    MODIFY COLUMN owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL COMMENT '备忘记录者所属用户，由当前助理的可信用户身份确定',
    DROP COLUMN visibility,
    DROP COLUMN uploaded_at,
    ADD PRIMARY KEY (owner_userid, file_id),
    ADD KEY idx_assistant_file_notes_owner_updated (owner_userid, updated_at, file_id),
    ADD KEY idx_assistant_file_notes_file (file_id),
    ADD CONSTRAINT fk_assistant_file_notes_file FOREIGN KEY (file_id)
        REFERENCES uploaded_files (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    COMMENT='个人助理附件备忘，每位用户对同一文件最多一条记录';
