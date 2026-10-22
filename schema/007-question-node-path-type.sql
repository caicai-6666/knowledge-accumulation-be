-- 在 001 之后手工执行；应用模型及持锁写入已适配，应用不自动执行迁移。
-- 与 kind（任务/摘要）独立；main 为主干路径，branch 为分支路径。
-- 默认 main 不会识别历史分支；非空旧表需先准备按主干父链回填的配套迁移。
-- 本文件仅补字段，不执行历史回填；部署前须与配套回填和应用升级一起安排。
ALTER TABLE question_nodes
    ADD COLUMN path_type ENUM('main','branch') NOT NULL DEFAULT 'main'
        COMMENT 'main：主干节点；branch：分支节点；与任务/摘要类型独立' AFTER kind;
