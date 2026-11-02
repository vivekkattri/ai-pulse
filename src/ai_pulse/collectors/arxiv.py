"""arXiv collector: fetches the newest papers and stores each version in raw.arxiv_papers."""

import logging
import time
from datetime import datetime
from xml.etree.ElementTree import Element, register_namespace, tostring

import httpx
import psycopg
from defusedxml import ElementTree
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from ai_pulse.config import ArxivConfig, load_arxiv_config
from ai_pulse.db import connect

logger = logging.getLogger(__name__)

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# Keep the original prefixes when an <entry> is turned back into XML text.
register_namespace("", NS["atom"])
register_namespace("opensearch", NS["opensearch"])
register_namespace("arxiv", NS["arxiv"])

INSERT_SQL = """
    INSERT INTO raw.arxiv_papers (arxiv_id, version, payload, entry_xml, source_url)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (arxiv_id, version) DO NOTHING
"""


class ArxivPaper(BaseModel):
    """The fields the collector checks. The full entry XML is stored anyway."""

    arxiv_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    title: str | None = None
    published: datetime | None = None
    updated: datetime | None = None
    authors: list[str] = []
    categories: list[str] = []


def describe_validation_error(error: ValidationError) -> str:
    """Turn a pydantic error into one short line, e.g. 'version: Input should be ...'."""
    problems = []
    for problem in error.errors():
        field = ".".join(str(part) for part in problem["loc"])
        problems.append(f"{field}: {problem['msg']}")
    return "; ".join(problems)


def fetch_page(client: httpx.Client, config: ArxivConfig, start: int) -> httpx.Response:
    """GET one page of results. Retries network errors, 429 and 5xx."""
    params = {
        "search_query": config.search_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": config.page_size,
    }
    for attempt in range(1, config.max_retries + 1):
        try:
            response = client.get(config.base_url, params=params)
        except httpx.TransportError as error:
            problem = f"network error ({error.__class__.__name__})"
        else:
            if response.status_code not in RETRY_STATUS_CODES:
                response.raise_for_status()
                return response
            problem = f"status {response.status_code}"

        if attempt == config.max_retries:
            raise RuntimeError(
                f"giving up after {config.max_retries} attempts: {problem}"
            )
        wait_seconds = config.pause_seconds * 2 ** (attempt - 1)
        logger.warning(
            "%s for start=%d, retrying in %.0f s (attempt %d of %d)",
            problem,
            start,
            wait_seconds,
            attempt,
            config.max_retries,
        )
        time.sleep(wait_seconds)


def text_of(element: Element, path: str) -> str | None:
    """The text inside one child tag, or None if the tag is missing."""
    return element.findtext(path, namespaces=NS)


def attribute_of(element: Element, path: str, name: str) -> str | None:
    """One attribute of one child tag, or None if the tag is missing."""
    child = element.find(path, NS)
    if child is None:
        return None
    return child.get(name)


def parse_entry(entry: Element) -> dict:
    """Turn one <entry> into a dict with every field the collector reads."""
    entry_url = text_of(entry, "atom:id") or ""
    id_part = entry_url.split("/abs/")[-1]
    arxiv_id, _, version = id_part.rpartition("v")

    abs_url = None
    pdf_url = None
    for link in entry.findall("atom:link", NS):
        if link.get("rel") == "alternate":
            abs_url = link.get("href")
        elif link.get("title") == "pdf":
            pdf_url = link.get("href")

    return {
        "arxiv_id": arxiv_id,
        "version": version,
        "entry_url": entry_url,
        "title": text_of(entry, "atom:title"),
        "summary": text_of(entry, "atom:summary"),
        "published": text_of(entry, "atom:published"),
        "updated": text_of(entry, "atom:updated"),
        "authors": [
            author.findtext("atom:name", namespaces=NS)
            for author in entry.findall("atom:author", NS)
        ],
        "categories": [
            category.get("term") for category in entry.findall("atom:category", NS)
        ],
        "primary_category": attribute_of(entry, "arxiv:primary_category", "term"),
        "abs_url": abs_url,
        "pdf_url": pdf_url,
        "comment": text_of(entry, "arxiv:comment"),
        "journal_ref": text_of(entry, "arxiv:journal_ref"),
        "doi": text_of(entry, "arxiv:doi"),
    }


def parse_page(xml_text: str) -> tuple[int, list[Element]]:
    """Read one page of XML. Returns (total results for the search, list of <entry> tags)."""
    feed = ElementTree.fromstring(xml_text)
    total_text = feed.findtext("opensearch:totalResults", namespaces=NS)
    total_results = int(total_text) if total_text else 0
    entries = feed.findall("atom:entry", NS)

    if len(entries) == 1 and "/api/errors" in (text_of(entries[0], "atom:id") or ""):
        message = text_of(entries[0], "atom:summary")
        raise RuntimeError(f"arXiv rejected the query: {message}")

    return total_results, entries


def collect_papers(
    client: httpx.Client, config: ArxivConfig
) -> tuple[list[tuple[str, int, dict, str, str]], int]:
    """Page through the results. Returns (good papers, number skipped)."""
    papers = []
    skipped = 0
    start = 0

    while len(papers) < config.max_items:
        if start > 0:
            time.sleep(config.pause_seconds)

        response = fetch_page(client, config, start)
        source_url = str(response.url)
        total_results, entries = parse_page(response.text)
        logger.info(
            "Page start=%d: %d entries (search has %d results)",
            start,
            len(entries),
            total_results,
        )

        for entry in entries:
            payload = parse_entry(entry)
            try:
                paper = ArxivPaper.model_validate(payload)
            except ValidationError as error:
                logger.warning(
                    "Skipping entry %s: %s",
                    payload["entry_url"],
                    describe_validation_error(error),
                )
                skipped += 1
                continue
            payload["version"] = paper.version
            entry_xml = tostring(entry, encoding="unicode")
            papers.append(
                (paper.arxiv_id, paper.version, payload, entry_xml, source_url)
            )

        start += config.page_size
        if len(entries) < config.page_size or start >= total_results:
            break

    return papers[: config.max_items], skipped


def count_rows(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT count(*) FROM raw.arxiv_papers").fetchone()[0]


def save_papers(
    conn: psycopg.Connection, papers: list[tuple[str, int, dict, str, str]]
) -> None:
    rows = [
        (arxiv_id, version, Jsonb(payload), entry_xml, source_url)
        for arxiv_id, version, payload, entry_xml, source_url in papers
    ]
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)


def run() -> None:
    config = load_arxiv_config()
    started = time.monotonic()
    logger.info("Starting arXiv collector (up to %d papers)", config.max_items)

    headers = {"User-Agent": "ai-pulse/0.1 (data engineering learning project)"}
    with httpx.Client(
        timeout=config.request_timeout_seconds, headers=headers, follow_redirects=True
    ) as client:
        papers, skipped = collect_papers(client, config)

    with connect() as conn:
        rows_before = count_rows(conn)
        save_papers(conn, papers)
        rows_after = count_rows(conn)

    logger.info(
        "Finished in %.1f s: %d papers checked (%d new rows), %d skipped, table has %d rows",
        time.monotonic() - started,
        len(papers),
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
