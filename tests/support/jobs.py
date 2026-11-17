from django.db import connection

from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding


def calculate(binding: SiteBinding, target_date: object, data: ProviderData) -> dict[str, object]:
    assert not connection.in_atomic_block
    assert data.collected_at is not None
    return {"energy_kwh_m2": None, "complete": False, "flags": ["incomplete_day"]}
