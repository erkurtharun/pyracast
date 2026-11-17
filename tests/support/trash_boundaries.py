from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from typing import Any

import pytest
from django.db import close_old_connections, connection
from django.db.models import Model, QuerySet

from pyracast.regions.models import Region


def _race_before_region_lock(
    monkeypatch: pytest.MonkeyPatch,
    query_method: str,
    work: Callable[[], object],
    purge: Callable[[], None],
    *,
    lock_model: type[Model] = Region,
) -> object:
    entered, release = Event(), Event()
    main_thread = get_ident()
    original = getattr(QuerySet, query_method)

    def paused(queryset: QuerySet[Any], *args: Any, **kwargs: Any) -> Any:
        if (
            get_ident() != main_thread
            and queryset.model is lock_model
            and queryset.query.select_for_update
        ):
            entered.set()
            assert release.wait(timeout=10)
        return original(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, query_method, paused)

    def worker() -> object:
        close_old_connections()
        try:
            return work()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(timeout=10)
            purge()
        finally:
            release.set()
        return future.result(timeout=10)
