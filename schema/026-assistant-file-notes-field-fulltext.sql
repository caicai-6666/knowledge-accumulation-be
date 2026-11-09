-- 在 025 后手工执行；名称和摘要分别匹配，供 AND/OR 组合使用。
-- 保留原联合索引兼容已有查询，不自动迁移或删除数据。
ALTER TABLE assistant_file_notes
    ADD FULLTEXT INDEX ft_assistant_file_notes_name (name) WITH PARSER ngram;

-- InnoDB 每条 ALTER 只能创建一个 FULLTEXT 索引，两步之间不具备事务原子性。
ALTER TABLE assistant_file_notes
    ADD FULLTEXT INDEX ft_assistant_file_notes_summary (summary) WITH PARSER ngram;
