BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS owner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    claimed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    settings TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_migrations (
    old_chat_id INTEGER PRIMARY KEY,
    new_chat_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    actor_id INTEGER,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    ends_at INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    retry_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS user_actions ON actions (chat_id, user_id, id DESC);
CREATE INDEX IF NOT EXISTS pending_actions ON actions (retry_at, id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    ends_at INTEGER,
    revoked_at INTEGER,
    auto_action_id INTEGER REFERENCES actions (id)
);
CREATE INDEX IF NOT EXISTS user_warnings ON warnings (chat_id, user_id, id DESC);
CREATE INDEX IF NOT EXISTS warning_batch ON warnings (auto_action_id) WHERE auto_action_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS restrictions (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    action_id INTEGER NOT NULL REFERENCES actions (id),
    ends_at INTEGER,
    telegram_until INTEGER NOT NULL,
    retry_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id, kind)
);
CREATE INDEX IF NOT EXISTS due_restrictions ON restrictions (retry_at) WHERE ends_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS activities (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    last_day TEXT,
    current INTEGER NOT NULL DEFAULT 0,
    best INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS recent_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    counted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS recent_by_topic ON recent_messages (chat_id, thread_id, message_id DESC);
CREATE INDEX IF NOT EXISTS recent_by_date ON recent_messages (sent_at);

PRAGMA user_version = 1;
COMMIT;
