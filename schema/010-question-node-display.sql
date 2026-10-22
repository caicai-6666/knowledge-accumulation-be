-- 在现有问答树迁移后手工执行；独立于模型上下文正文，旧节点保持 NULL。
ALTER TABLE question_node_contents
    ADD COLUMN display_history JSON NULL,
    ADD CONSTRAINT chk_question_display_object
        CHECK (display_history IS NULL OR JSON_TYPE(display_history) = 'OBJECT');
