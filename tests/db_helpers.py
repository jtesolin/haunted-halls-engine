from alembic import command
from alembic.config import Config


def migrate_database() -> None:
    command.upgrade(Config("alembic.ini"), "head")