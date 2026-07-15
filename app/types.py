"""A timezone-safe DateTime column type.

SQLite does not persist timezone info (it stores datetimes as naive strings),
which means values read back are offset-naive while values from the API layer
are offset-aware — and comparing the two raises TypeError. This decorator
normalizes every datetime to timezone-aware UTC on the way in and out, so the
rest of the code can assume aware-UTC everywhere, on both SQLite and Postgres.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
