"""Hacker News collector: fetches the newest stories and stores them in raw.hn_items."""

import logging
import time
from datetime import datetime

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from ai_pulse.config import HackerNewsConfig, load_hackernews_config
from ai_pulse.db import connect

logger = logging.getLogger(__name__)

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

UPSERT_SQL = """
    INSERT INTO raw.hn_items (item_id, payload, source_url)
    VALUES (%s, %s, %s)
    ON CONFLICT (item_id) DO UPDATE
    SET payload = EXCLUDED.payload,
        source_url = EXCLUDED.source_url,
        updated_at = now()
    WHERE raw.hn_items.payload IS DISTINCT FROM EXCLUDED.payload
"""


class HNItem(BaseModel):
    """The fields the collector checks. The full original JSON is stored anyway."""

    id: int = Field(gt=0)
    type: str | None = None
    by: str | None = None
    time: datetime | None = None
    title: str | None = None
    url: str | None = None
    score: int | None = None
    descendants: int | None = None
    deleted: bool = False
    dead: bool = False


def fetch_json(client: httpx.Client, url: str, max_retries: int):
    """GET a URL and return the parsed JSON. Retries network errors, 429 and 5xx."""
    for attempt in range(1, max_retries + 1):
        try:
            response = client.get(url)
        except httpx.TransportError as error:
            problem = f"network error ({error.__class__.__name__})"
        else:
            if response.status_code not in RETRY_STATUS_CODES:
                response.raise_for_status()
                return response.json()
            problem = f"status {response.status_code}"

        if attempt == max_retries:
            raise RuntimeError(f"giving up after {max_retries} attempts: {problem}")
        wait_seconds = 2 ** (attempt - 1)
        logger.warning(
            "%s for %s, retrying in %d s (attempt %d of %d)",
            problem,
            url,
            wait_seconds,
            attempt,
            max_retries,
        )
        time.sleep(wait_seconds)


def describe_validation_error(error: ValidationError) -> str:
    """Turn a pydantic error into one short line, e.g. 'score: Input should be a valid integer'."""
    problems = []
    for problem in error.errors():
        field = ".".join(str(part) for part in problem["loc"])
        problems.append(f"{field}: {problem['msg']}")
    return "; ".join(problems)


def fetch_story_ids(client: httpx.Client, config: HackerNewsConfig) -> list[int]:
    url = f"{config.base_url}/{config.list_endpoint}.json"
    data = fetch_json(client, url, config.max_retries)
    story_ids = TypeAdapter(list[int]).validate_python(data)
    return story_ids[: config.max_items]


def fetch_items(
    client: httpx.Client, config: HackerNewsConfig, story_ids: list[int]
) -> tuple[list[tuple[int, dict, str]], int]:
    """Fetch and validate each item. Returns (good items, number skipped)."""
    items = []
    skipped = 0
    for story_id in story_ids:
        url = f"{config.base_url}/item/{story_id}.json"
        try:
            payload = fetch_json(client, url, config.max_retries)
        except (httpx.HTTPStatusError, RuntimeError, ValueError) as error:
            logger.warning(
                "Could not fetch item %s: %s", story_id, str(error).splitlines()[0]
            )
            skipped += 1
            continue

        if payload is None:
            logger.warning("Item %s returned null, skipping it", story_id)
            skipped += 1
            continue

        try:
            item = HNItem.model_validate(payload)
        except ValidationError as error:
            logger.warning(
                "Item %s failed validation, skipping it: %s",
                story_id,
                describe_validation_error(error),
            )
            skipped += 1
            continue

        if item.id != story_id:
            logger.warning(
                "Asked for item %s but got item %s, skipping it", story_id, item.id
            )
            skipped += 1
            continue

        items.append((item.id, payload, url))
        time.sleep(config.pause_seconds)

    return items, skipped


def count_rows(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT count(*) FROM raw.hn_items").fetchone()[0]


def save_items(conn: psycopg.Connection, items: list[tuple[int, dict, str]]) -> None:
    rows = [(item_id, Jsonb(payload), url) for item_id, payload, url in items]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)


def run() -> None:
    config = load_hackernews_config()
    started = time.monotonic()
    logger.info("Starting Hacker News collector (up to %d items)", config.max_items)

    headers = {"User-Agent": "ai-pulse/0.1 (data engineering learning project)"}
    with httpx.Client(
        timeout=config.request_timeout_seconds, headers=headers, follow_redirects=True
    ) as client:
        story_ids = fetch_story_ids(client, config)
        logger.info("Got %d story IDs from /%s", len(story_ids), config.list_endpoint)
        items, skipped = fetch_items(client, config, story_ids)

    with connect() as conn:
        rows_before = count_rows(conn)
        save_items(conn, items)
        rows_after = count_rows(conn)

    logger.info(
        "Finished in %.1f s: %d valid items upserted (%d new rows), %d skipped, table has %d rows",
        time.monotonic() - started,
        len(items),
        rows_after - rows_before,
        skipped,
        rows_after,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    run()


if __name__ == "__main__":
    main()
