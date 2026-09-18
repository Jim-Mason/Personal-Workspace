-- LocalDeck 初始表结构
-- 设计原则：一切被管理对象都是 resource，一切动作都是 task，一切变化都留 event 流水。

-- ---------------------------------------------------------------- resource
-- 统一资源表。应用 / 文件 / 文件夹 / 网址 / 脚本 / 环境项共用一张表，
-- 靠 type 区分，避免每加一类就新建一张表。
CREATE TABLE IF NOT EXISTS resource (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    type             TEXT    NOT NULL,                 -- app | file | folder | url | script | env
    name             TEXT    NOT NULL,
    path_or_url      TEXT,                             -- 目标路径或网址
    args             TEXT,                             -- 启动参数，JSON 数组字符串
    alias            TEXT,                             -- 别名，参与搜索
    source           TEXT    NOT NULL DEFAULT 'manual', -- registry | startmenu | scan | manual
    source_key       TEXT,                             -- 来源内唯一键，用于幂等重扫
    group_name       TEXT,                             -- 所属分组
    tags             TEXT,                             -- 标签，JSON 数组字符串
    icon             TEXT,                             -- 图标（路径或 data URI）
    -- 应用专属元数据（从注册表带出来）
    version          TEXT,
    publisher        TEXT,
    install_date     TEXT,
    install_location TEXT,
    uninstall_string TEXT,
    size_bytes       INTEGER,                          -- 占用字节；none 表示未知
    size_source      TEXT,                             -- registry | dirwalk | unknown
    -- 使用行为
    last_used_at     TEXT,
    use_count        INTEGER NOT NULL DEFAULT 0,
    favorite         INTEGER NOT NULL DEFAULT 0,
    -- 生命周期
    managed          INTEGER NOT NULL DEFAULT 0,       -- 1=用户手动纳管，扫描不得覆盖
    ignored          INTEGER NOT NULL DEFAULT 0,       -- 1=用户已忽略，不再出现在默认列表
    enabled          INTEGER NOT NULL DEFAULT 1,
    note             TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 幂等重扫的关键：同一来源 + 同一来源键 = 同一资源，重复扫描只更新不新增。
CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_source_key
    ON resource(source, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_resource_type   ON resource(type);
CREATE INDEX IF NOT EXISTS ix_resource_name   ON resource(name);
CREATE INDEX IF NOT EXISTS ix_resource_used   ON resource(last_used_at);

-- ---------------------------------------------------------------- task
-- 每一次执行（启动应用 / 扫描 / 跑脚本）都留一条任务记录。
CREATE TABLE IF NOT EXISTS task (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,                      -- launch | scan | execute | export
    resource_id INTEGER REFERENCES resource(id) ON DELETE SET NULL,
    item_name   TEXT,                                  -- 冗余存名字，资源被删后流水仍可读
    params      TEXT,                                  -- JSON
    status      TEXT    NOT NULL DEFAULT 'pending',    -- pending | running | ok | failed | skipped
    exit_code   INTEGER,
    started_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    duration_ms INTEGER,
    machine_id  TEXT    NOT NULL,                      -- 机器标识：多机场景下必须能区分
    output_path TEXT,                                  -- 产物归档路径
    message     TEXT
);
CREATE INDEX IF NOT EXISTS ix_task_resource ON task(resource_id);
CREATE INDEX IF NOT EXISTS ix_task_started  ON task(started_at);

-- ---------------------------------------------------------------- event
-- 审计流水。所有写操作都要留痕，"谁在什么时候动了什么"必须能精确还原。
CREATE TABLE IF NOT EXISTS event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL DEFAULT 'local',         -- local | web
    action      TEXT NOT NULL,                         -- scan.start | app.launch | app.ignore | ...
    target_type TEXT,
    target_id   INTEGER,
    target_name TEXT,
    detail      TEXT,                                  -- JSON，含变更前后差异
    ip          TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_event_action  ON event(action);
CREATE INDEX IF NOT EXISTS ix_event_created ON event(created_at);

-- ---------------------------------------------------------------- meta
-- 键值表：迁移版本、机器标识等。
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
