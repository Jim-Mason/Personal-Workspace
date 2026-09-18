-- 0002_seed_cards.sql
-- 初始卡片数据（对应设计稿中的 9 个功能）
--
-- ⚠️ 这里的 target_url 全部是**示例地址**（192.168.1.x），
--    登录后请到「功能管理」里改成你自己真实的内网地址与端口。
--    使用 INSERT OR IGNORE + slug 唯一约束，重复执行不会产生重复数据。

INSERT OR IGNORE INTO cards
    (slug, title, description, icon, icon_style, target_url, open_mode, group_name,
     sort_order, enabled, open_in_new_tab, click_count, verify_tls, created_at, updated_at)
VALUES
    ('address-nav', '地址导航',
     '集中查看原型与内部系统入口。',
     '址', 'blue', 'http://192.168.1.10:8080/', 'proxy', '常用工具', 10, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('token-switch', 'Token 切换用户',
     '输入 token 查询当前登录用户，并把 Redis 缓存中的用户 ID 和用户信息切换成指定用户。',
     'U', 'indigo', 'http://192.168.1.11:8080/', 'proxy', '常用工具', 20, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('dict-entry', '字典录入',
     '选择系统 appId，填写字典名称、字典标识和 JSON 字典项，快速录入通用字典数据。',
     'D', 'violet', 'http://192.168.1.12:8080/', 'proxy', '常用工具', 30, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('menu-permission', '前端菜单权限录入',
     '录入前端菜单、按钮和权限点配置，当前为页面占位，后续可继续扩展表单功能。',
     '#', 'cyan', 'http://192.168.1.13:8080/', 'proxy', '常用工具', 40, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('version-branch', '查询版本开发分支',
     '输入目标分支名称，快速确认本次开发涉及哪些仓库已经创建了对应开发分支。',
     'Y', 'teal', 'http://192.168.1.14:8080/', 'proxy', '常用工具', 50, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('code-generator', '生成代码',
     '根据数据库表结构自动生成基础服务、Facade 与业务侧代码，并下载 ZIP 包。',
     '{}', 'blue', 'http://192.168.1.15:8080/', 'proxy', '常用工具', 60, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('zentao-bug-push', '禅道 Bug 推送配置',
     '在线维护禅道 bug 邮件推送人规则，支持查询、新增和删除推送配置。',
     '@', 'amber', 'http://192.168.1.16:8080/', 'proxy', '常用工具', 70, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('device-migration-sql', '设备迁移 SQL 生成器',
     '选择原客户、设备和目标客户，校验当前归属后生成跨库设备迁移更新 SQL。',
     'SQL', 'rose', 'http://192.168.1.17:8080/', 'proxy', '常用工具', 80, 1, 1, 0, NULL, datetime('now'), datetime('now')),

    ('file-preview', '文件预览',
     '上传文件，可以在线预览。',
     '文', 'green', 'http://192.168.1.18:8080/', 'proxy', '常用工具', 90, 1, 1, 0, NULL, datetime('now'), datetime('now'));
