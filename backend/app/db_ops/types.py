"""Portable column types."""
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC storage with naive-UTC Python values.

    * PostgreSQL: real ``timestamptz`` column (unambiguous, independent of session timezone).
    * SQLite: stores the UTC wall-clock text (SQLite has no tz type).
    * Python side: ALWAYS returns naive UTC datetimes - this matches the app-wide
      ``utcnow()`` helpers (naive UTC) so comparisons never raise
      "can't compare offset-naive and offset-aware datetimes" on either engine.
    * Writes accept naive (assumed UTC) or aware (converted to UTC) datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):  # let the DB driver complain about junk
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
