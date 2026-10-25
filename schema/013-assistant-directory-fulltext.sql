-- 手工在权威人员库执行；该库必须已有 employees，不创建或复制人员资料。
-- MySQL 8.4 内置 ngram，不安装额外插件。应用启动不自动执行此迁移。
-- 已有同等索引时不要重复执行；真实环境执行前按部署流程核对索引与停用词配置。
ALTER TABLE employees
    ADD FULLTEXT INDEX ft_employee_name (name) WITH PARSER ngram;
