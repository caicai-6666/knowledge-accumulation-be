-- 在共享权威人员库手工执行；已有等价索引时先核对，不重复创建。
-- 只索引一级部门名称，不修改员工资料、归属或权限。
ALTER TABLE departments
    ADD FULLTEXT INDEX ft_department_name (name) WITH PARSER ngram;
