-- 手工迁移：按顺序执行既有 001—010 后再运行；不由应用启动自动执行。
-- 先核对同用户是否已有多棵 assistant 树；重复数据将使唯一索引创建失败，不能自动删除历史。
-- 函数索引不增加 SELECT * 可见列，不影响既有 QuestionTree 的严格字段校验。
CREATE UNIQUE INDEX uq_question_assistant_owner
    ON question_trees ((CASE WHEN kind='assistant' THEN owner_userid ELSE NULL END));
ALTER TABLE question_trees
    ADD UNIQUE KEY uq_question_tree_identity (id,owner_userid,kind);

CREATE TABLE assistants (
    userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin PRIMARY KEY,
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    tree_kind ENUM('assistant','exploration') NOT NULL DEFAULT 'assistant',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    heartbeat_interval_seconds INT NOT NULL DEFAULT -1,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_assistant_tree (tree_id),
    UNIQUE KEY uq_assistant_identity (userid,tree_id),
    INDEX idx_assistant_enabled (enabled,userid),
    CONSTRAINT fk_assistant_tree_identity FOREIGN KEY (tree_id,userid,tree_kind)
        REFERENCES question_trees(id,owner_userid,kind),
    CHECK (tree_kind='assistant'),
    CHECK (enabled IN (0,1)),
    CHECK (heartbeat_interval_seconds=-1 OR heartbeat_interval_seconds>0),
    CHECK (CHAR_LENGTH(TRIM(userid))>0),
    CHECK (updated_at>=created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 只记录已领取任务，不在本地复制优先级队列；task_id 来自中间件。
CREATE TABLE assistant_task_executions (
    task_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin PRIMARY KEY,
    userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    task_type ENUM('reminder','user_message','assistant_message','heartbeat') NOT NULL,
    input_payload JSON NOT NULL COMMENT 'pull 冻结的任务快照，不存平台 token 或交付凭证',
    file_bindings JSON NOT NULL COMMENT '中间件文件引用到本地任务附件的映射，原始消息归属在 input_payload',
    processing_status ENUM('received','prepared','running','completed','failed') NOT NULL DEFAULT 'received',
    error_code VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    received_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    acknowledged_at DATETIME(6) NULL,
    UNIQUE KEY uq_assistant_execution_identity (task_id,userid),
    UNIQUE KEY uq_assistant_execution_node (node_id),
    UNIQUE KEY uq_assistant_unacknowledged ((CASE WHEN acknowledged_at IS NULL THEN userid ELSE NULL END)),
    INDEX idx_assistant_execution_user (userid,received_at,task_id),
    INDEX idx_assistant_execution_recovery (acknowledged_at,processing_status,updated_at),
    CONSTRAINT fk_assistant_execution_owner FOREIGN KEY (userid,tree_id) REFERENCES assistants(userid,tree_id),
    CONSTRAINT fk_assistant_execution_node FOREIGN KEY (tree_id,node_id) REFERENCES question_nodes(tree_id,id),
    CHECK (CHAR_LENGTH(TRIM(task_id))>0),
    CHECK (JSON_TYPE(input_payload)='OBJECT'),
    CHECK (JSON_CONTAINS_PATH(input_payload,'all','$.task_id','$.userid','$.task_type','$.files','$.user_messages')=1),
    CHECK (JSON_UNQUOTE(JSON_EXTRACT(input_payload,'$.task_id')) COLLATE utf8mb4_0900_bin=task_id),
    CHECK (JSON_UNQUOTE(JSON_EXTRACT(input_payload,'$.userid')) COLLATE utf8mb4_0900_bin=userid),
    CHECK (JSON_UNQUOTE(JSON_EXTRACT(input_payload,'$.task_type'))=task_type),
    CHECK (JSON_TYPE(file_bindings)='ARRAY'),
    CHECK (updated_at>=received_at),
    CHECK ((processing_status IN ('completed','failed'))=(finished_at IS NOT NULL)),
    CHECK (finished_at IS NULL OR (finished_at>=received_at AND finished_at<=updated_at)),
    CHECK (processing_status NOT IN ('running','completed') OR node_id IS NOT NULL),
    CHECK (acknowledged_at IS NULL OR (finished_at IS NOT NULL AND acknowledged_at>=finished_at AND acknowledged_at<=updated_at)),
    CHECK (error_code IS NULL OR CHAR_LENGTH(TRIM(error_code))>0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE assistant_reminders (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    content TEXT NOT NULL,
    schedule_type ENUM('once','interval','calendar') NOT NULL,
    schedule_config JSON NOT NULL COMMENT '按 schedule_type 严格验证的一次性、固定间隔或日历规则',
    timezone VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'Asia/Shanghai',
    next_fire_at DATETIME(6) NULL,
    end_at DATETIME(6) NULL,
    status ENUM('enabled','paused','completed','cancelled') NOT NULL DEFAULT 'enabled',
    created_by_task_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_assistant_reminder_identity (id,userid),
    INDEX idx_assistant_reminder_due (status,next_fire_at,id),
    INDEX idx_assistant_reminder_user (userid,status,id),
    CONSTRAINT fk_assistant_reminder_owner FOREIGN KEY (userid) REFERENCES assistants(userid),
    CONSTRAINT fk_assistant_reminder_source FOREIGN KEY (created_by_task_id,userid)
        REFERENCES assistant_task_executions(task_id,userid),
    CHECK (CHAR_LENGTH(TRIM(content)) BETWEEN 1 AND 10000),
    CHECK (JSON_TYPE(schedule_config)='OBJECT'),
    CHECK (CHAR_LENGTH(TRIM(timezone))>0),
    CHECK (status<>'enabled' OR next_fire_at IS NOT NULL),
    CHECK (status NOT IN ('completed','cancelled') OR next_fire_at IS NULL),
    CHECK (end_at IS NULL OR next_fire_at IS NULL OR next_fire_at<=end_at),
    CHECK (updated_at>=created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 可靠交付记录兼容任务发送动作与提醒到期发布；不是中间件结果队列。
-- 未来调度须在同一事务写入本表并推进提醒的 next_fire_at，再由发送器执行外部请求。
CREATE TABLE assistant_deliveries (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    task_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL,
    reminder_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    action_key VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    scheduled_for DATETIME(6) NULL,
    destination ENUM('user','assistant') NOT NULL,
    recipient_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    task_type ENUM('assistant_message','reminder') NULL,
    message LONGTEXT NOT NULL,
    status ENUM('pending','sent','failed') NOT NULL DEFAULT 'pending',
    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt_at DATETIME(6) NULL,
    sent_at DATETIME(6) NULL,
    external_receipt JSON NULL,
    error_code VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_assistant_delivery_action (task_id,action_key),
    UNIQUE KEY uq_assistant_reminder_occurrence (reminder_id,scheduled_for),
    INDEX idx_assistant_delivery_pending (status,next_attempt_at,id),
    INDEX idx_assistant_delivery_user (userid,created_at,id),
    CONSTRAINT fk_assistant_delivery_owner FOREIGN KEY (userid) REFERENCES assistants(userid),
    CONSTRAINT fk_assistant_delivery_task FOREIGN KEY (task_id,userid) REFERENCES assistant_task_executions(task_id,userid),
    CONSTRAINT fk_assistant_delivery_reminder FOREIGN KEY (reminder_id,userid) REFERENCES assistant_reminders(id,userid),
    CHECK ((task_id IS NOT NULL)+(reminder_id IS NOT NULL)=1),
    CHECK ((reminder_id IS NOT NULL)=(scheduled_for IS NOT NULL)),
    CHECK (CHAR_LENGTH(TRIM(action_key))>0),
    CHECK (CHAR_LENGTH(TRIM(recipient_userid))>0),
    CHECK (CHAR_LENGTH(TRIM(message))>0),
    CHECK (destination<>'assistant' OR CHAR_LENGTH(message)<=10000),
    CHECK ((destination='user' AND task_type IS NULL) OR (destination='assistant' AND task_type IS NOT NULL)),
    CHECK (reminder_id IS NULL OR (destination='assistant' AND task_type='reminder' AND recipient_userid=userid)),
    CHECK (task_id IS NULL OR destination='user' OR task_type='assistant_message'),
    CHECK ((status='sent')=(sent_at IS NOT NULL)),
    CHECK (sent_at IS NULL OR (sent_at>=created_at AND sent_at<=updated_at)),
    CHECK (status<>'sent' OR next_attempt_at IS NULL),
    CHECK (external_receipt IS NULL OR JSON_TYPE(external_receipt)='OBJECT'),
    CHECK (error_code IS NULL OR CHAR_LENGTH(TRIM(error_code))>0),
    CHECK (updated_at>=created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
