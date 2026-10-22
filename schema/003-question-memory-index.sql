-- 可重建的 MyVector 缓存，与 registry 的 myvector_documents 使用不同表及索引。
-- 仅用于已安装 MyVector 的 MySQL；维度/容量须与部署配置一致。
CREATE TABLE question_memory_index (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    memory_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    owner_userid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    tree_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    node_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    kind ENUM('question','intermediate','final') NOT NULL,
    embedding BLOB NOT NULL COMMENT 'MYVECTOR COLUMN |type=HNSW,dim=4096,size=10000,dist=Cosine,M=16,ef=100,idcol=id',
    UNIQUE KEY uq_question_memory_index (memory_id),
    INDEX idx_question_memory_index_scope (owner_userid,tree_id,kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
