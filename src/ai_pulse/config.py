"""Loads settings: secrets from .env, normal settings from config/sources.yaml."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCES_FILE = PROJECT_ROOT / "config" / "sources.yaml"


class HackerNewsConfig(BaseModel):
    base_url: str
    list_endpoint: str
    max_items: int = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0)
    pause_seconds: float = Field(ge=0)
    max_retries: int = Field(ge=1)


def get_database_url() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "DATABASE_URL is missing. Add it to the .env file in the project folder."
        )
    return database_url


def load_hackernews_config() -> HackerNewsConfig:
    with open(SOURCES_FILE, encoding="utf-8") as file:
        sources = yaml.safe_load(file)
    return HackerNewsConfig.model_validate(sources["hackernews"])
