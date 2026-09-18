-- 0004_card_endpoints.down.sql
-- 回滚多环境地址

DROP INDEX IF EXISTS idx_card_endpoints_slug;

DROP INDEX IF EXISTS idx_card_endpoints_card;

DROP TABLE IF EXISTS card_endpoints;
