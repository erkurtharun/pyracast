from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects
from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("connections", "0003_provider_account_and_binding_checks")]

    operations: ClassVar[list[Operation]] = [
        RenameInPlace(
            model_name="provideraccount",
            old_name="canonical_provider_account",
            item=models.UniqueConstraint(
                models.Func(
                    models.Func(
                        "provider",
                        models.Value(
                            " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002"
                            "\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029"
                            "\u202f\u205f\u3000"
                        ),
                        function="BTRIM",
                    ),
                    function="CASEFOLD",
                    output_field=models.CharField(),
                    template='%(function)s(%(expressions)s COLLATE "pg_unicode_fast")',
                ),
                models.Func(
                    models.Func(
                        "external_account_id",
                        models.Value(
                            " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002"
                            "\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029"
                            "\u202f\u205f\u3000"
                        ),
                        function="BTRIM",
                    ),
                    function="CASEFOLD",
                    output_field=models.CharField(),
                    template='%(function)s(%(expressions)s COLLATE "pg_unicode_fast")',
                ),
                name="provideraccount_provider_external_account_id_uniq",
            ),
        ),
        RenameInPlace(
            model_name="provideraccount",
            old_name="provider_account_encrypted_api_key",
            item=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("encrypted_api_key", ""), ("provider", "fixture")),
                    ("encrypted_api_key__startswith", "encrypted:v1:"),
                    _connector="OR",
                ),
                name="provideraccount_encrypted_api_key_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_region_identity",
            item=models.UniqueConstraint(
                fields=("id", "region"), name="sitebinding_id_region_uniq"
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_account_identity",
            item=models.UniqueConstraint(
                fields=("id", "provider_account"), name="sitebinding_id_provider_account_uniq"
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_capacity",
            item=models.CheckConstraint(
                condition=models.Q(("capacity_kw__gt", 0)),
                name="sitebinding_capacity_kw_positive_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_capacity_finite",
            item=models.CheckConstraint(
                condition=models.Q(("capacity_kw__lt", float("inf"))),
                name="sitebinding_capacity_kw_finite_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_dc_capacity",
            item=models.CheckConstraint(
                condition=models.Q(
                    ("dc_capacity_kw__isnull", True),
                    models.Q(("dc_capacity_kw__gt", 0), ("dc_capacity_kw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="sitebinding_dc_capacity_kw_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_latitude",
            item=models.CheckConstraint(
                condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                name="sitebinding_latitude_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_longitude",
            item=models.CheckConstraint(
                condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                name="sitebinding_longitude_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_power_basis",
            item=models.CheckConstraint(
                condition=models.Q(("power_basis__in", ["ac", "dc"])),
                name="sitebinding_power_basis_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_azimuth",
            item=models.CheckConstraint(
                condition=models.Q(("azimuth_degrees__gte", 0), ("azimuth_degrees__lt", 360)),
                name="sitebinding_azimuth_degrees_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_losses",
            item=models.CheckConstraint(
                condition=models.Q(("losses_percent__gte", 0), ("losses_percent__lt", 100)),
                name="sitebinding_losses_percent_chk",
            ),
        ),
        RenameInPlace(
            model_name="sitebinding",
            old_name="binding_health",
            item=models.CheckConstraint(
                condition=models.Q(("health__in", ["unknown", "healthy", "error"])),
                name="sitebinding_health_chk",
            ),
        ),
        *schema_objects.operations("connections", "0004_schema_naming_convention"),
    ]
