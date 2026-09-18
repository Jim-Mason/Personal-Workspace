-- 0001_init.sql
-- 初始表结构：用户 / 卡片 / 审计日志
-- 方言：SQLite（如改用 PostgreSQL/MySQL，请按对应方言新增迁移文件）

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER      PRIMARY KEY,
    uid           VARCHAR(32)  NOT NULL UNIQUE,
    username      VARCHAR(64)  NOT NULL UNIQUE,
    display_name  VARCHAR(64)  NOT NULL DEFAULT '',
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(16)  NOT NULL DEFAULT 'user',
    is_active     BOOLEAN      NOT NULL DEFAULT 1,
    last_login_at DATETIME     NULL,
    created_at    DATETIME     NOT NULL,
    updated_at    DATETIME     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);

CREATE TABLE IF NOT EXISTS cards (
    id              INTEGER       PRIMARY KEY,
    slug            VARCHAR(64)   NOT NULL UNIQUE,
    title           VARCHAR(128)  NOT NULL,
    description     VARCHAR(512)  NOT NULL DEFAULT '',
    icon            VARCHAR(16)   NOT NULL DEFAULT '',
    icon_style      VARCHAR(16)   NOT NULL DEFAULT 'blue',
    target_url      VARCHAR(1024) NOT NULL,
    open_mode       VARCHAR(16)   NOT NULL DEFAULT 'proxy',
    group_name      VARCHAR(64)   NOT NULL DEFAULT '默认分组',
    sort_order      INTEGER       NOT NULL DEFAULT 0,
    enabled         BOOLEAN       NOT NULL DEFAULT 1,
    open_in_new_tab BOOLEAN       NOT NULL DEFAULT 1,
    click_count     INTEGER       NOT NULL DEFAULT 0,
    verify_tls      BOOLEAN       NULL,
    created_at      DATETIME      NOT NULL,
    updated_at      DATETIME      NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_slug ON cards (slug);
CREATE INDEX IF NOT EXISTS idx_cards_enabled_sort ON cards (enabled, sort_order);
CREATE INDEX IF NOT EXISTS idx_cards_group ON cards (group_name);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER     PRIMARY KEY,
    user_id     INTEGER     NULL REFERENCES users (id) ON DELETE SET NULL,
    username    VARCHAR(64) NOT NULL DEFAULT '',
    action      VARCHAR(64) NOT NULL,
    target_type VARCHAR(32) NOT NULL DEFAULT '',
    target_id   VARCHAR(64) NOT NULL DEFAULT '',
    detail      TEXT        NOT NULL DEFAULT '',
    ip          VARCHAR(64) NOT NULL DEFAULT '',
    created_at  DATETIME    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs (action);
