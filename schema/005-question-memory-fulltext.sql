-- 在 001、002、003、004 后手工执行，不修改权威消息/记忆表。
-- 旧缓存正文为空，执行后由显式 refresh 从权威源重建。
ALTER TABLE question_memory_index
    ADD COLUMN text LONGTEXT NOT NULL,
    ADD FULLTEXT INDEX ft_question_memory_text (text) WITH PARSER ngram;
