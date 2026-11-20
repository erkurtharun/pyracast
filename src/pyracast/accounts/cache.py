import json
from collections.abc import Iterable
from typing import Any

from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.backends.utils import CursorWrapper


class AuthenticationCache(BaseCache):
    def __init__(self, _location: str, params: dict[str, Any]) -> None:
        super().__init__(params)

    @staticmethod
    def _cursor() -> CursorWrapper:
        connection = connections["auth_cache"]
        if not connection.get_autocommit():
            raise ImproperlyConfigured(
                "The authentication cache requires an autocommit connection."
            )
        return connection.cursor()

    def _write(self, key: str, value: Any, timeout: Any, version: int | None, *, add: bool) -> bool:
        cache_key = self.make_and_validate_key(key, version=version)
        lifetime = self.default_timeout if timeout is DEFAULT_TIMEOUT else timeout
        encoded = json.dumps(value, allow_nan=False)
        with self._cursor() as cursor:
            cursor.execute(
                "WITH expired AS ("
                "SELECT cache_key FROM pyracast_authentication_cache "
                "WHERE expires <= clock_timestamp() ORDER BY expires LIMIT 100 "
                "FOR UPDATE SKIP LOCKED) "
                "DELETE FROM pyracast_authentication_cache USING expired "
                "WHERE pyracast_authentication_cache.cache_key = expired.cache_key"
            )
            cursor.execute(
                "INSERT INTO pyracast_authentication_cache (cache_key, value, expires) "
                "VALUES (%s, %s::jsonb, "
                "clock_timestamp() + %s::double precision * interval '1 second') "
                "ON CONFLICT (cache_key) DO UPDATE SET value = EXCLUDED.value, "
                "expires = EXCLUDED.expires "
                "WHERE NOT %s OR pyracast_authentication_cache.expires <= clock_timestamp() "
                "RETURNING cache_key",
                [cache_key, encoded, lifetime, add],
            )
            return cursor.fetchone() is not None

    def add(
        self, key: str, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: int | None = None
    ) -> bool:
        return self._write(key, value, timeout, version, add=True)

    def set(
        self, key: str, value: Any, timeout: Any = DEFAULT_TIMEOUT, version: int | None = None
    ) -> None:
        self._write(key, value, timeout, version, add=False)

    def get(self, key: str, default: Any = None, version: int | None = None) -> Any:
        cache_key = self.make_and_validate_key(key, version=version)
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT value::text FROM pyracast_authentication_cache "
                "WHERE cache_key = %s AND (expires IS NULL OR expires > clock_timestamp())",
                [cache_key],
            )
            row = cursor.fetchone()
        return json.loads(row[0]) if row else default

    def get_many(self, keys: Iterable[str], version: int | None = None) -> dict[str, Any]:
        stored = {self.make_and_validate_key(key, version=version): key for key in keys}
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT cache_key, value::text FROM pyracast_authentication_cache "
                "WHERE cache_key = ANY(%s) AND (expires IS NULL OR expires > clock_timestamp())",
                [list(stored)],
            )
            return {stored[cache_key]: json.loads(value) for cache_key, value in cursor.fetchall()}

    def delete(self, key: str, version: int | None = None) -> bool:
        cache_key = self.make_and_validate_key(key, version=version)
        with self._cursor() as cursor:
            cursor.execute(
                "DELETE FROM pyracast_authentication_cache WHERE cache_key = %s", [cache_key]
            )
            return bool(cursor.rowcount)

    def clear(self) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM pyracast_authentication_cache")
