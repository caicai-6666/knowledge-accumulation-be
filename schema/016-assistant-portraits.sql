-- 手工在权威人员库执行；依赖 employees 和 013 姓名 ngram 全文索引，应用不自动迁移。
-- key 为全表稳定自增标识；业务不得重置计数器、重排或复用已删除 key。
CREATE TABLE assistant_portraits (
    `key` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    subject_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    preference TEXT NOT NULL,
    PRIMARY KEY (`key`),
    KEY idx_portraits_owner_subject (owner_userid, subject_userid, `key`),
    CONSTRAINT fk_portraits_owner FOREIGN KEY (owner_userid) REFERENCES employees(userid),
    CONSTRAINT fk_portraits_subject FOREIGN KEY (subject_userid) REFERENCES employees(userid),
    CONSTRAINT chk_portraits_preference CHECK (CHAR_LENGTH(TRIM(preference)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
