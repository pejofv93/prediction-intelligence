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

-- Uso diario de tokens por proveedor LLM (para rotación inteligente)
CREATE TABLE IF NOT EXISTS llm_usage (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    provider    TEXT      NOT NULL,
    tokens      INTEGER   NOT NULL DEFAULT 0,
    day         TEXT      NOT NULL,  -- formato YYYY-MM-DD (UTC)
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_day
    ON llm_usage (provider, day);

-- Tabla de precios históricos para calcular cambio 24h (ARGOS)
CREATE TABLE IF NOT EXISTS oracle_prices (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    coin        TEXT      NOT NULL,
    price       REAL      NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oracle_prices_coin_time
    ON oracle_prices (coin, recorded_at);

-- Noticias procesadas por PYTHIA
CREATE TABLE IF NOT EXISTS oracle_news (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    title       TEXT      NOT NULL,
    url         TEXT      UNIQUE,
    source      TEXT,
    published   TIMESTAMP,
    score       REAL      DEFAULT 0,
    topic       TEXT,
    pipeline_id TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cola A/B thumbnail swap (ALETHEIA + KAIROS)
CREATE TABLE IF NOT EXISTS ab_swap_queue (
    id               INTEGER   PRIMARY KEY AUTOINCREMENT,
    pipeline_id      TEXT      NOT NULL,
    youtube_video_id TEXT      NOT NULL,
    check_at         TIMESTAMP NOT NULL,
    status           TEXT      DEFAULT 'pending',
    winner           TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabla de migraciones de schema (para columnas añadidas incrementalmente)
CREATE TABLE IF NOT EXISTS _schema_migrations (
    id      INTEGER   PRIMARY KEY AUTOINCREMENT,
    name    TEXT      UNIQUE,
    applied TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Registro de ejecuciones del VOLUME_GUARDIAN (KAIROS 03:00 UTC)
CREATE TABLE IF NOT EXISTS volume_cleanup_log (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    freed_bytes INTEGER   DEFAULT 0,
    action      TEXT,
    disk_pct    REAL,
    ran_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NOTA: Las siguientes columnas se añaden via db.py execute_schema()
-- si aún no existen (compatibilidad con DBs antiguas):
--   videos.avg_view_percentage REAL
--   videos.avg_duration_seconds REAL
--   videos.watch_time_minutes REAL
--   videos.impressions INTEGER DEFAULT 0
--   videos.ctr REAL DEFAULT 0
