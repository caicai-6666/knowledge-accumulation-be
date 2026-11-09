-- 在 024 之后手工执行一次；应用不自动迁移。
-- 名称与摘要共同参与全文召回，使用 MySQL 内置 ngram 支持中文字符片检索。
-- 查询使用 MATCH(name, summary)，并由可信身份限定 owner_userid。
ALTER TABLE assistant_file_notes
    ADD FULLTEXT INDEX ft_assistant_file_notes_name_summary (name, summary) WITH PARSER ngram;
