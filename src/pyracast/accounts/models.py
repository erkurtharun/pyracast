from django.conf import settings
from django.db import models


class Tenant(models.Model):
    name = models.CharField(max_length=200)


class Membership(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("tenant", "user"), name="unique_tenant_membership"),
        )


class AuthenticationCacheEntry(models.Model):
    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.JSONField()
    expires = models.DateTimeField(null=True, db_index=True)

    class Meta:
        db_table = "pyracast_authentication_cache"
