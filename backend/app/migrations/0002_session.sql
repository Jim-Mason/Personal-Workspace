-- 浏览器会话：让「关掉页面再打开」不必重新输令牌。
--
-- 为什么另建一张表，而不是塞进 meta 的 key-value：
-- 会话是**多条、会自动过期、要成批清理**的东西，塞进 key-value
-- 只会变成一堆没人敢删的键。
--
-- id 存的是**会话号的 sha256**，不是会话号本身：
--   Cookie 里发出去的是原串，只有它能在库里兑出记录；
--   反过来，光拿到 localdeck.db 这个文件是登不进来的。
--
-- token_fp 是「签发这个会话时用的令牌」的指纹。它的用处是：
-- 你重置令牌（删掉 data/.token 重启）时，所有旧会话**立即集体失效** ——
-- 否则令牌换了、别人的浏览器却还留着一条合法会话，那才是真的漏洞。

CREATE TABLE IF NOT EXISTS session (
    id           TEXT PRIMARY KEY,
    token_fp     TEXT NOT NULL,
    user_agent   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expires_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_expires ON session (expires_at);
