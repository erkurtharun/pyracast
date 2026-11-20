from collections.abc import Iterator
from contextlib import contextmanager

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import CommandError
from django.db import OperationalError
from psycopg.errors import LockNotAvailable

from pyracast.errors import DomainError
from pyracast.plants.models import Plant, active_plant


def operator_scope(username: str, plant_id: int) -> tuple[User, int, int, int]:
    actor = User.objects.filter(username=username).first()
    if actor is None:
        raise CommandError(f"User {username} not found.")
    plant = Plant.objects.select_related("region").filter(active_plant(), pk=plant_id).first()
    if plant is None:
        raise CommandError(f"Plant {plant_id} not found.")
    return actor, plant.region.tenant_id, plant.region_id, plant.pk


@contextmanager
def refusals(action: str, username: str, plant_id: int) -> Iterator[None]:
    try:
        yield
    except PermissionDenied as error:
        raise CommandError(f"User {username} may not change plant {plant_id}.") from error
    except DomainError as error:
        raise CommandError(f"{action} refused: {error.code.value}.") from error
    except ValidationError as error:
        raise CommandError(f"{action} refused: {' '.join(error.messages)}") from error
    except OperationalError as error:
        if isinstance(error.__cause__, LockNotAvailable):
            raise CommandError("Workspace busy; retry.") from error
        raise
