from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        ("connections", "0001_initial"),
        ("regions", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="sitebinding",
            name="region",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="bindings",
                to="regions.region",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.UniqueConstraint(
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
                name="canonical_provider_account",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("encrypted_api_key", ""), ("provider", "fixture")),
                    ("encrypted_api_key__startswith", "encrypted:v1:"),
                    _connector="OR",
                ),
                name="provider_account_encrypted_api_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("provider__in", ["fixture", "solcast"])),
                name="provideraccount_provider_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.UniqueConstraint(
                fields=("id", "region"), name="binding_region_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.UniqueConstraint(
                fields=("id", "provider_account"), name="binding_account_identity"
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("capacity_kw__gt", 0)), name="binding_capacity"
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("capacity_kw__lt", float("inf"))),
                name="binding_capacity_finite",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("dc_capacity_kw__isnull", True),
                    models.Q(("dc_capacity_kw__gt", 0), ("dc_capacity_kw__lt", float("inf"))),
                    _connector="OR",
                ),
                name="binding_dc_capacity",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("latitude__gte", -90), ("latitude__lte", 90)),
                name="binding_latitude",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("longitude__gte", -180), ("longitude__lte", 180)),
                name="binding_longitude",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("power_basis__in", ["ac", "dc"])), name="binding_power_basis"
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("azimuth_degrees__gte", 0), ("azimuth_degrees__lt", 360)),
                name="binding_azimuth",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("losses_percent__gte", 0), ("losses_percent__lt", 100)),
                name="binding_losses",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitebinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("health__in", ["unknown", "healthy", "error"])),
                name="binding_health",
            ),
        ),
        *schema_objects.operations("connections", "0002_initial"),
    ]
