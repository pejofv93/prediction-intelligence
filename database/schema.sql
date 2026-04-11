-- NEXUS v1.0 · CryptoVerdad · SQLite Schema

CREATE TABLE IF NOT EXISTS pipelines (
    id           TEXT      PRIMARY KEY,
    topic        TEXT      NOT NULL,
    mode         TEXT      DEFAULT 'standard',
    status       TEXT      DEFAULT 'pending',
    youtube_url  TEXT,
    tiktok_url   TEXT,
    seo_score    INTEGER,
    errors       TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS videos (
    id               TEXT      PRIMARY KEY,
    pipeline_id      TEXT,
    platform         TEXT,
    video_id         TEXT,
    title            TEXT,
    url              TEXT,
    views            INTEGER   DEFAULT 0,
    likes            INTEGER   DEFAULT 0,
    thumbnail_winner TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (pipeline_id) REFERENCES pipelines(id)
);

CREATE TABLE IF NOT EXISTS learning_data (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    video_id    TEXT,
    metric      TEXT,
    value       REAL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS optimal_hours (
    day_of_week  INTEGER,
    hour         INTEGER,
    avg_views    REAL,
    sample_size  INTEGER,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (day_of_week, hour)
);

CREATE TABLE IF NOT EXISTS memoria_videos (
    video_id       TEXT      PRIMARY KEY,
    title          TEXT      NOT NULL,
    url            TEXT      NOT NULL,
    seo_score      INTEGER   DEFAULT 0,
    published_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    privacy_status TEXT      DEFAULT 'public'
);

CREATE TABLE IF NOT EXISTS telegram_notifications (
    id           INTEGER   PRIMARY KEY AUTOINCREMENT,
    pipeline_id  TEXT,
    chat_id      TEXT,
    message_id   INTEGER,
    message_text TEXT,
    sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_prices (
    coin_id     TEXT      PRIMARY KEY,
    price_usd   REAL      NOT NULL,
    updated_at  TEXT      NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_comments (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    video_id    TEXT,
    comment_id  TEXT      UNIQUE,
    author      TEXT,
    text        TEXT,
    reply_text  TEXT,
    replied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pipeline_id TEXT
);
