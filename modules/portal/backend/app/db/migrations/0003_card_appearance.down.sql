-- 0003_card_appearance.down.sql
-- 回滚卡片外观字段（SQLite 3.35+ 支持 DROP COLUMN）

ALTER TABLE cards DROP COLUMN accent_color;
ALTER TABLE cards DROP COLUMN bg_style;
ALTER TABLE cards DROP COLUMN icon_url;
