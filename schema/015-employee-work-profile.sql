-- 在权威人员库手工执行一次；应用不自动迁移，不覆盖已有员工资料。
-- 历史员工使用两个 null 表示尚未采集，后续登录不会自动重置或补采。
ALTER TABLE employees
    ADD COLUMN work_profile JSON NOT NULL
        DEFAULT (JSON_OBJECT('manager_userid', NULL, 'title', NULL))
        COMMENT '任职资料：直属主管userid、职位名称',
    ADD CONSTRAINT chk_employees_work_profile CHECK (
        JSON_SCHEMA_VALID(
            '{"type":"object","properties":{"manager_userid":{"type":["string","null"],"maxLength":128},"title":{"type":["string","null"],"maxLength":200}},"required":["manager_userid","title"],"additionalProperties":false}',
            work_profile
        )
    );
