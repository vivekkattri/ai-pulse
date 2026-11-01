"""Opens connections to the AI Pulse PostgreSQL database."""

import psycopg

from ai_pulse.config import get_database_url


def connect() -> psycopg.Connection:
    return psycopg.connect(get_database_url())
