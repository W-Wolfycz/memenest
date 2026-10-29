-- 表情窝 · 库结构（schema_version = 2）
-- 约定：rel_path 相对插件数据目录，正斜杠分隔；所有时间列为 Unix 秒。

CREATE TABLE IF NOT EXISTS memo (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path     TEXT    NOT NULL UNIQUE,
    content_hash TEXT    NOT NULL,
    percept_hash TEXT,
    label        TEXT    NOT NULL,
    caption      TEXT    NOT NULL DEFAULT '',
    text_in_img  TEXT    NOT NULL DEFAULT '',
    scope        TEXT    NOT NULL DEFAULT 'any',
    origin_umo   TEXT,
    state        TEXT    NOT NULL DEFAULT 'inbox',
    is_fav       INTEGER NOT NULL DEFAULT 0,
    use_count    INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    used_at      INTEGER NOT NULL DEFAULT 0,
    origin_kind  TEXT,
    persona_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_memo_state_created ON memo(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memo_hash ON memo(content_hash);
CREATE INDEX IF NOT EXISTS idx_memo_label ON memo(label);
CREATE INDEX IF NOT EXISTS idx_memo_used ON memo(used_at);

CREATE TABLE IF NOT EXISTS memo_scene (
    memo_id INTEGER NOT NULL REFERENCES memo(id) ON DELETE CASCADE,
    scene   TEXT    NOT NULL,
    PRIMARY KEY (memo_id, scene)
);

-- v4：语义检索向量（model 进主键，换模型时新旧共存，不互相污染）
-- 一条表情按内容分成几路分别建向量：同一条可以有 text/caption/scene 三行。
-- 检索时各路单独算相似度再加权，混成一根向量会把弱势那路稀释掉。
CREATE TABLE IF NOT EXISTS memo_embedding (
    memo_id    INTEGER NOT NULL REFERENCES memo(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (memo_id, kind, model)
);

CREATE INDEX IF NOT EXISTS idx_memo_embedding_model ON memo_embedding(model);

-- v3：一条表情可以绑多个人格；一条都不绑视为通用
CREATE TABLE IF NOT EXISTS memo_persona (
    memo_id    INTEGER NOT NULL REFERENCES memo(id) ON DELETE CASCADE,
    persona_id TEXT    NOT NULL,
    PRIMARY KEY (memo_id, persona_id)
);

CREATE INDEX IF NOT EXISTS idx_memo_persona_pid ON memo_persona(persona_id);

-- v2：附加主标签（同一条表情覆盖多种情绪；label 列仍是主标签）
CREATE TABLE IF NOT EXISTS memo_label (
    memo_id INTEGER NOT NULL REFERENCES memo(id) ON DELETE CASCADE,
    label   TEXT    NOT NULL,
    PRIMARY KEY (memo_id, label)
);

CREATE INDEX IF NOT EXISTS idx_memo_label_label ON memo_label(label);

CREATE TABLE IF NOT EXISTS blocklist (
    content_hash TEXT    PRIMARY KEY,
    created_at   INTEGER NOT NULL,
    -- 拉黑时的 200px 缩略图（JPEG 字节）。拉黑会把原图删掉，
    -- 存一份小图才能让人看出「当初拉黑的是哪张」，指纹本身对人没有意义。
    thumb        BLOB
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
