-- Grain: one row per version of one arXiv paper, exactly as arXiv described it.
-- A new version (v2, v3, ...) becomes a new row; existing rows are never changed.
CREATE TABLE IF NOT EXISTS raw.arxiv_papers (
    arxiv_id       text         NOT NULL,
    version        integer      NOT NULL,
    payload        jsonb        NOT NULL,
    entry_xml      text         NOT NULL,
    source_url     text         NOT NULL,
    first_seen_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (arxiv_id, version)
);

COMMENT ON TABLE raw.arxiv_papers IS
    'One row per arXiv paper version: parsed fields as JSONB plus the original <entry> XML';
