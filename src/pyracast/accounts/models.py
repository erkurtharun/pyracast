from django.conf import settings
from django.db import models

from pyracast.db import choices_check


class Language(models.TextChoices):
    EN = "en", "English"
    TR = "tr", "Türkçe"


class WorkspaceRole(models.TextChoices):
    ADMIN = "admin", "Administrator"
    VIEWER = "viewer", "Viewer"


class InvitationDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    UNCERTAIN = "uncertain", "Uncertain"


class Tenant(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(name__regex=r"^\s*$"), name="workspace_name_not_blank"
            ),
        )


class Membership(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=6, choices=WorkspaceRole, default=WorkspaceRole.VIEWER)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("tenant", "user"), name="unique_tenant_membership"),
            models.UniqueConstraint(
                fields=("tenant",),
                condition=models.Q(role=WorkspaceRole.ADMIN),
                name="one_workspace_admin",
            ),
            choices_check("role", WorkspaceRole, name="valid_workspace_role"),
        )


class WorkspaceInvitation(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    delivery_status = models.CharField(
        max_length=9, choices=InvitationDeliveryStatus, default=InvitationDeliveryStatus.PENDING
    )
    role = models.CharField(max_length=6, choices=WorkspaceRole, default=WorkspaceRole.VIEWER)

    class Meta:
        constraints = (
            choices_check("delivery_status", InvitationDeliveryStatus),
            choices_check("role", WorkspaceRole),
            # Readers treat an invitation as open while `expires_at` lies ahead, so a revocation
            # ends the deadline too.
            models.CheckConstraint(
                condition=models.Q(revoked_at__isnull=True)
                | models.Q(accepted_at__isnull=True, expires_at__lte=models.F("revoked_at")),
                name="%(class)s_revoked_at_chk",
            ),
            models.UniqueConstraint(
                fields=("tenant", "email"),
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="%(class)s_tenant_email_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant",),
                condition=models.Q(
                    role=WorkspaceRole.ADMIN, accepted_at__isnull=True, revoked_at__isnull=True
                ),
                name="%(class)s_tenant_administrator_uniq",
            ),
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

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(administrator=models.F("previous_administrator")),
                name="%(class)s_administrator_chk",
            ),
        )


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
    language = models.CharField(max_length=2, choices=Language)

    class Meta:
        constraints = (choices_check("language", Language, name="supported_user_language"),)
