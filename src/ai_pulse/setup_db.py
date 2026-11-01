"""Creates the database schemas and tables by running every file in sql/ in order."""

import logging

from ai_pulse.config import PROJECT_ROOT
from ai_pulse.db import connect

logger = logging.getLogger(__name__)

SQL_DIR = PROJECT_ROOT / "sql"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sql_files = sorted(SQL_DIR.glob("*.sql"))
    with connect() as conn:
        for path in sql_files:
            logger.info("Running %s", path.name)
            conn.execute(path.read_text(encoding="utf-8"))
    logger.info("Database is set up (%d SQL files)", len(sql_files))


if __name__ == "__main__":
    main()
