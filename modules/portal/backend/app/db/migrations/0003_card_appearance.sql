-- 0003_card_appearance.sql
-- 卡片自定义能力：图标图片、卡片底色预设、图标主色
--
-- 说明：SQLite 的 ALTER TABLE ADD COLUMN 若声明 NOT NULL 必须带 DEFAULT，
--       这里统一用空字符串表示"未自定义"，由应用层决定回退到默认样式。

ALTER TABLE cards ADD COLUMN icon_url VARCHAR(512) NOT NULL DEFAULT '';
ALTER TABLE cards ADD COLUMN bg_style VARCHAR(24) NOT NULL DEFAULT '';
ALTER TABLE cards ADD COLUMN accent_color VARCHAR(9) NOT NULL DEFAULT '';
