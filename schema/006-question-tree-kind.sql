-- 在 001 之后手工执行；现有问答树保持复杂任务探索的分支语义。
-- 新库也需执行本迁移；应用不会自动建表或修改真实数据库。
-- kind 仅区分用途；两类树共享节点结构，助理不开放节点选择由后续接口约束。
ALTER TABLE question_trees
    ADD COLUMN kind ENUM('assistant','exploration') NOT NULL DEFAULT 'exploration'
        COMMENT 'assistant：个人助理；exploration：复杂任务探索；共用树结构' AFTER name,
    ADD INDEX idx_question_trees_owner_kind_updated (owner_userid,kind,updated_at,id);
