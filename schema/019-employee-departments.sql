-- 在共享权威人员库执行一次；知识后端与注册中心均需部署支持此字段的代码。
-- 保留旧单值列和外键；NULL兼容旧客户端写入，读取时回退为旧单值的数组。
ALTER TABLE employees
    ADD COLUMN department_ids JSON NULL COMMENT '全部一级部门ID；单值department_id仅供旧接口兼容',
    ADD CONSTRAINT chk_employees_department_ids CHECK (
        department_ids IS NULL OR JSON_SCHEMA_VALID(
            '{"type":"array","minItems":1,"uniqueItems":true,"items":{"type":"integer","minimum":0}}', department_ids
        )
    );
