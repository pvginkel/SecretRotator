"""Postgres, as much of it as the cnpg-role kind uses: a login as a role, which proves its
password."""

import psycopg

from secret_rotator.model import StepFailed

DATABASE = "postgres"
TIMEOUT = 10  # seconds


class PostgresError(StepFailed):
    """A login that failed, refused or not reached. A StepFailed, so a step reports it by its one
    sentence, which carries libpq's own error and never the password."""


def login(host: str, port: int, user: str, password: str) -> None:
    """Logs in as the user and out again. sslmode require, as Terraform reaches the server: the
    connection is encrypted, and the server's name is not verified."""
    try:
        with psycopg.connect(
            host=host,
            port=port,
            dbname=DATABASE,
            user=user,
            password=password,
            sslmode="require",
            connect_timeout=TIMEOUT,
        ):
            pass
    except psycopg.OperationalError as e:
        said = " ".join(str(e).split())
        raise PostgresError(f"the login to {host}:{port} as {user} fails: {said}") from None
