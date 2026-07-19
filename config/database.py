"""Database connection initialization for the SQLite single-node deployment."""

from __future__ import annotations

from django.db.backends.signals import connection_created


def configure_sqlite_connection(sender, connection, **kwargs) -> None:
    """Set SQLite connection invariants required by the single-worker deployment."""
    if connection.vendor != "sqlite":
        return

    timeout_ms = int(connection.settings_dict["OPTIONS"].get("timeout", 5) * 1000)
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")


connection_created.connect(configure_sqlite_connection)
