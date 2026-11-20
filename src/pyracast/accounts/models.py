from datetime import datetime
from typing import Self

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


class InvitationState(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"
    SUPERSEDED = "superseded", "Superseded"


class InvitationRevocation(models.TextChoices):
    CANCELLED = "cancelled", "Cancelled"
    SUPERSEDED = "superseded", "Superseded"
    MEMBER_REMOVED = "member_removed", "Member removed"
    ADMINISTRATOR_CHANGED = "administrator_changed", "Administrator changed"


class Tenant(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(name__regex=r"^\s*$"), name="%(class)s_name_chk"
            ),
        )

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=6, choices=WorkspaceRole, default=WorkspaceRole.VIEWER)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("tenant", "user"), name="%(class)s_tenant_user_uniq"),
            models.UniqueConstraint(
                fields=("tenant",),
                condition=models.Q(role=WorkspaceRole.ADMIN),
                name="%(class)s_tenant_administrator_uniq",
            ),
            choices_check("role", WorkspaceRole),
        )


class WorkspaceInvitationQuerySet[M: WorkspaceInvitation = WorkspaceInvitation, Row = M](
    models.QuerySet[M, Row]
):
    def open(self) -> Self:
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True)

    def pending(self, now: datetime) -> Self:
        return self.open().filter(expires_at__gt=now)

    def revoke(self, now: datetime, reason: InvitationRevocation) -> int:
        return self.open().update(revoked_at=now, revoked_reason=reason)


class WorkspaceInvitation(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    email = models.EmailField()
    # The digest of the newest token the mailer minted; none until it first prepares the mail.
    token_hash = models.CharField(max_length=64, unique=True, null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    # The natural deadline; closing an invitation never moves it.
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True)
    # Empty on rows revoked before reasons were stored.
    revoked_reason = models.CharField(
        max_length=21, choices=InvitationRevocation, blank=True, default=""
    )
    delivery_status = models.CharField(
        max_length=9, choices=InvitationDeliveryStatus, default=InvitationDeliveryStatus.PENDING
    )
    role = models.CharField(max_length=6, choices=WorkspaceRole, default=WorkspaceRole.VIEWER)

    objects = WorkspaceInvitationQuerySet.as_manager()

    class Meta:
        constraints = (
            choices_check("delivery_status", InvitationDeliveryStatus),
            choices_check("role", WorkspaceRole),
            choices_check("revoked_reason", InvitationRevocation, blank=True),
            models.CheckConstraint(
                condition=models.Q(revoked_at__isnull=True, revoked_reason="")
                | models.Q(revoked_at__isnull=False, accepted_at__isnull=True),
                name="%(class)s_revoked_at_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(accepted_by__isnull=True) | models.Q(accepted_at__isnull=False),
                name="%(class)s_accepted_by_chk",
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

    def state_at(self, now: datetime) -> InvitationState:
        if self.accepted_at is not None:
            return InvitationState.ACCEPTED
        if self.revoked_reason == InvitationRevocation.SUPERSEDED:
            return InvitationState.SUPERSEDED
        if self.revoked_at is not None:
            return InvitationState.REVOKED
        return InvitationState.PENDING if self.expires_at > now else InvitationState.EXPIRED


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
        constraints = (choices_check("language", Language),)
