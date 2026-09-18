-- 0004_card_endpoints.sql
-- 卡片多环境地址：一张卡片可以挂多条「环境名称 + URL」，每条可独立选择直连或代理
--
-- 设计说明（为什么每条地址要自己的 slug）：
--   每条地址分配一个全局唯一的网关入口标识，形如 tms-dev。
--   这样 /gw/{slug}/ 只需要按标识解析出「卡片」或「环境地址」两种目标之一，
--   网关前缀计算、HTML/CSS 重写、Cookie 命名空间等逻辑一行都不用改 ——
--   它们本来就只依赖 (slug, target_url) 这两个值。
--   slug 与 cards.slug 的全局唯一由应用层保证：SQLite 无法跨表建唯一约束。
--
-- 为什么不用「一张卡片一个 JSON 列」：
--   环境地址需要按标识单独查询（网关解析）、需要独立排序，拆表才能建索引。

CREATE TABLE IF NOT EXISTS card_endpoints (
    id          INTEGER       PRIMARY KEY,
    card_id     INTEGER       NOT NULL REFERENCES cards (id) ON DELETE CASCADE,
    slug        VARCHAR(80)   NOT NULL UNIQUE,
    name        VARCHAR(64)   NOT NULL,
    url         VARCHAR(1024) NOT NULL,
    open_mode   VARCHAR(16)   NOT NULL DEFAULT 'direct',
    verify_tls  BOOLEAN       NULL,
    sort_order  INTEGER       NOT NULL DEFAULT 0,
    created_at  DATETIME      NOT NULL,
    updated_at  DATETIME      NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_card_endpoints_card ON card_endpoints (card_id, sort_order);

CREATE INDEX IF NOT EXISTS idx_card_endpoints_slug ON card_endpoints (slug);
