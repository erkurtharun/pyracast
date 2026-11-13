from django.db import models

# Match Python str.strip(); PostgreSQL btrim() otherwise strips only ASCII spaces.
IDENTITY_WHITESPACE = (
    " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def canonical_account_identity(expression: str | models.Expression | models.F) -> models.Func:
    stripped = models.Func(expression, models.Value(IDENTITY_WHITESPACE), function="BTRIM")
    return models.Func(
        stripped,
        function="CASEFOLD",
        template='%(function)s(%(expressions)s COLLATE "pg_unicode_fast")',
        output_field=models.CharField(),
    )
