-- 在权威人员库手工执行一次；应用不自动迁移。
-- 新建及现有员工默认在职；实际已离职的历史员工须按权威名单另行标记。
-- 默认值只用于插入，不会在重复建档或读取时重置已有离职标识。
ALTER TABLE employees
    ADD COLUMN employment_status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin
        NOT NULL DEFAULT 'employed' COMMENT '员工在职状态：employed在职，resigned离职',
    ADD CONSTRAINT chk_employees_employment_status
        CHECK (CAST(employment_status AS BINARY) IN ('employed', 'resigned'));
