from django.conf import settings
from django.db import models
from django.db.models.functions import Lower, Trim


class Tenant(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        constraints = (
            models.UniqueConstraint(Lower(Trim("name")), name="unique_workspace_name"),
            models.CheckConstraint(
                condition=~models.Q(name__regex=r"^\s*$"), name="workspace_name_not_blank"
            ),
        )


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        VIEWER = "viewer", "Viewer"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=6, choices=Role, default=Role.VIEWER)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("tenant", "user"), name="unique_tenant_membership"),
            models.UniqueConstraint(
                fields=("tenant",), condition=models.Q(role="admin"), name="one_workspace_admin"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=("admin", "viewer")), name="valid_workspace_role"
            ),
        )


class WorkspaceInvitation(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    delivery_status = models.CharField(
        max_length=9,
        choices=(
            ("pending", "Pending"),
            ("accepted", "Accepted"),
            ("failed", "Failed"),
            ("uncertain", "Uncertain"),
        ),
        default="pending",
    )


class WorkspaceAdminChange(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workspace_admin_changes"
    )
    administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workspace_admin_assignments",
    )
    previous_administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="previous_workspace_admin_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class WorkspaceMemberRemoval(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workspace_member_removals"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workspace_removals"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class AuthenticationCacheEntry(models.Model):
    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.JSONField()
    expires = models.DateTimeField(null=True, db_index=True)

    class Meta:
        db_table = "pyracast_authentication_cache"


class UserLanguagePreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    language = models.CharField(max_length=2, choices=(("en", "English"), ("tr", "Türkçe")))

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(language__in=("en", "tr")), name="supported_user_language"
            ),
        )
