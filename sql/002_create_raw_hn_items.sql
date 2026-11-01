-- Grain: one row per Hacker News item (story).
-- payload holds the latest response from /v0/item/<id>.json, unchanged, as JSONB.
CREATE TABLE IF NOT EXISTS raw.hn_items (
    item_id        bigint       PRIMARY KEY,
    payload        jsonb        NOT NULL,
    source_url     text         NOT NULL,
    first_seen_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON TABLE raw.hn_items IS
    'One row per Hacker News item: the latest API response, stored unchanged as JSONB';
