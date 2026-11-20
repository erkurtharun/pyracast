from typing import cast

from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.accounts.invitations import (
    accept_workspace_invitation,
    invite_workspace_member,
    revoke_workspace_invitation,
)
from pyracast.accounts.models import Membership, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import remove_workspace_member
from pyracast.api.serializers import InputSerializer
from pyracast.notifications.overview import plants_at_recipient_limit


class InvitationInput(InputSerializer):
    email = serializers.EmailField(max_length=254)


class InvitationAcceptanceInput(InputSerializer):
    token = serializers.CharField(max_length=128)


class InvitationOutput(serializers.ModelSerializer[WorkspaceInvitation]):
    class Meta:
        model = WorkspaceInvitation
        fields = ("id", "email", "expires_at", "delivery_status")
        read_only_fields = fields


class WorkspaceMemberOutput(serializers.Serializer[dict[str, object]]):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=WorkspaceRole.choices)


class WorkspaceMembersOutput(serializers.Serializer[dict[str, object]]):
    members = WorkspaceMemberOutput(many=True)
    plants_at_recipient_limit = serializers.IntegerField()


class WorkspaceInvitationsOutput(serializers.Serializer[dict[str, object]]):
    invitations = InvitationOutput(many=True)


class InvitationAcceptanceOutput(serializers.Serializer[dict[str, object]]):
    tenant_id = serializers.IntegerField()
    role = serializers.ChoiceField(choices=WorkspaceRole.choices)


class WorkspaceMembersView(APIView):
    @extend_schema(responses=WorkspaceMembersOutput)
    def get(self, request: Request, tenant_id: int) -> Response:
        memberships = (
            Membership.objects.filter(tenant_id=tenant_id, user__is_active=True)
            .select_related("user")
            .order_by("user_id")
        )
        return Response(
            {
                "members": [
                    {"id": member.user_id, "email": member.user.email, "role": member.role}
                    for member in memberships
                ],
                "plants_at_recipient_limit": plants_at_recipient_limit(tenant_id),
            }
        )


class WorkspaceMemberView(APIView):
    @extend_schema(responses={204: None})
    def delete(self, request: Request, tenant_id: int, user_id: int) -> Response:
        remove_workspace_member(cast(User, request.user), tenant_id, user_id)
        return Response(status=204)


class WorkspaceInvitationsView(APIView):
    @extend_schema(responses=WorkspaceInvitationsOutput)
    def get(self, request: Request, tenant_id: int) -> Response:
        invitations = WorkspaceInvitation.objects.filter(
            tenant_id=tenant_id, accepted_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("id")
        return Response({"invitations": InvitationOutput(invitations, many=True).data})

    @extend_schema(request=InvitationInput, responses={201: InvitationOutput})
    def post(self, request: Request, tenant_id: int) -> Response:
        data = InvitationInput(data=request.data)
        data.is_valid(raise_exception=True)
        invitation = invite_workspace_member(
            cast(User, request.user), tenant_id, data.validated_data["email"]
        )
        return Response(InvitationOutput(invitation).data, status=201)


class WorkspaceInvitationView(APIView):
    @extend_schema(responses={204: None})
    def delete(self, request: Request, tenant_id: int, invitation_id: int) -> Response:
        revoke_workspace_invitation(cast(User, request.user), tenant_id, invitation_id)
        return Response(status=204)


class InvitationAcceptanceView(APIView):
    @extend_schema(request=InvitationAcceptanceInput, responses=InvitationAcceptanceOutput)
    def post(self, request: Request) -> Response:
        data = InvitationAcceptanceInput(data=request.data)
        data.is_valid(raise_exception=True)
        membership = accept_workspace_invitation(
            cast(User, request.user), data.validated_data["token"]
        )
        return Response({"tenant_id": membership.tenant_id, "role": membership.role})
