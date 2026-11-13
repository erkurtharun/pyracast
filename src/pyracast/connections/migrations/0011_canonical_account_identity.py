from typing import ClassVar

from django.db import migrations, models

WHITESPACE = (
    " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0010_remove_sitebinding_binding_matching_power_basis_and_more"),
    ]

    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="provideraccount", name="canonical_provider_account"
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.UniqueConstraint(
                models.Func(
                    models.Func("provider", models.Value(WHITESPACE), function="BTRIM"),
                    function="CASEFOLD",
                    template='%(function)s(%(expressions)s COLLATE "pg_unicode_fast")',
                    output_field=models.CharField(),
                ),
                models.Func(
                    models.Func("external_account_id", models.Value(WHITESPACE), function="BTRIM"),
                    function="CASEFOLD",
                    template='%(function)s(%(expressions)s COLLATE "pg_unicode_fast")',
                    output_field=models.CharField(),
                ),
                name="canonical_provider_account",
            ),
        ),
    ]
