from datetime import timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.accounts.models import Tenant
from pyracast.accounts.operations import require_member
from pyracast.api.serializers import (
    AccountInput,
    ApprovalInput,
    BindingInput,
    ForecastOutput,
    ForecastQuery,
    IdentifierOutput,
    RegionInput,
    RegionOutput,
    RotateSecretInput,
    TenantOutput,
    TransitionInput,
)
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import create_account, rotate_secret, validate_binding
from pyracast.forecasts.models import ForecastResult
from pyracast.providers import adapter_for
from pyracast.regions.models import Region
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    submit_region,
    transition_region,
    update_region,
)


class PlatformAdmin(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        return bool(
            user.is_active and user.is_superuser and getattr(user, "is_verified", lambda: False)()
        )


class TenantsView(GenericAPIView[Tenant]):
    serializer_class = TenantOutput

    def get_queryset(self) -> QuerySet[Tenant]:
        return Tenant.objects.filter(membership__user=cast(User, self.request.user)).order_by("id")

    @extend_schema(responses=TenantOutput(many=True))
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(TenantOutput(page, many=True).data)


class RegionsView(GenericAPIView[Region]):
    serializer_class = RegionOutput

    def get_queryset(self) -> QuerySet[Region]:
        tenant_id = self.kwargs["tenant_id"]
        require_member(cast(User, self.request.user), tenant_id)
        return Region.objects.filter(tenant_id=tenant_id).order_by("id")

    @extend_schema(responses=RegionOutput(many=True))
    def get(self, request: Request, tenant_id: int) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(RegionOutput(page, many=True).data)

    @extend_schema(request=RegionInput, responses={201: RegionOutput})
    def post(self, request: Request, tenant_id: int) -> Response:
        data = RegionInput(data=request.data)
        data.is_valid(raise_exception=True)
        region = submit_region(cast(User, request.user), tenant_id, **data.validated_data)
        return Response(RegionOutput(region).data, status=201)


class RegionView(RegionsView):
    @extend_schema(responses=RegionOutput)
    def get(self, request: Request, tenant_id: int, region_id: int = 0) -> Response:
        return Response(RegionOutput(get_object_or_404(self.get_queryset(), pk=region_id)).data)

    @extend_schema(request=RegionInput, responses=RegionOutput)
    def put(self, request: Request, tenant_id: int, region_id: int) -> Response:
        get_object_or_404(self.get_queryset(), pk=region_id)
        data = RegionInput(data=request.data)
        data.is_valid(raise_exception=True)
        region = update_region(
            cast(User, request.user), region_id, tenant_id, **data.validated_data
        )
        return Response(RegionOutput(region).data)

    http_method_names = ["get", "put", "head", "options"]  # noqa: RUF012 -- Django class setting.


class AccountView(APIView):
    permission_classes = (PlatformAdmin,)

    @extend_schema(request=AccountInput, responses={201: IdentifierOutput})
    def post(self, request: Request) -> Response:
        data = AccountInput(data=request.data)
        data.is_valid(raise_exception=True)
        account = create_account(cast(User, request.user), **data.validated_data)
        return Response({"id": account.pk}, status=201)


class RotateSecretView(APIView):
    permission_classes = (PlatformAdmin,)

    @extend_schema(request=RotateSecretInput, responses=IdentifierOutput)
    def post(self, request: Request, account_id: int) -> Response:
        data = RotateSecretInput(data=request.data)
        data.is_valid(raise_exception=True)
        account = get_object_or_404(ProviderAccount, pk=account_id)
        rotate_secret(cast(User, request.user), account.pk, **data.validated_data)
        return Response({"id": account.pk})


class BindingView(APIView):
    permission_classes = (PlatformAdmin,)

    @extend_schema(request=BindingInput, responses={201: IdentifierOutput})
    def post(self, request: Request, region_id: int) -> Response:
        data = BindingInput(data=request.data)
        data.is_valid(raise_exception=True)
        binding = configure_binding(cast(User, request.user), region_id, **data.validated_data)
        return Response({"id": binding.pk}, status=201)


class ValidateView(APIView):
    permission_classes = (PlatformAdmin,)

    @extend_schema(request=None, responses=IdentifierOutput)
    def post(self, request: Request, binding_id: int) -> Response:
        binding = get_object_or_404(
            SiteBinding.objects.select_related("provider_account"), pk=binding_id
        )
        validate_binding(cast(User, request.user), binding_id, adapter_for(binding), timezone.now())
        return Response({"id": binding_id})


class ApproveView(APIView):
    permission_classes = (PlatformAdmin,)

    @extend_schema(request=ApprovalInput, responses=IdentifierOutput)
    def post(self, request: Request, region_id: int) -> Response:
        data = ApprovalInput(data=request.data)
        data.is_valid(raise_exception=True)
        job = approve_region(
            cast(User, request.user), region_id, now=timezone.now(), **data.validated_data
        )
        return Response({"id": job.pk})


class TransitionView(APIView):
    permission_classes = (PlatformAdmin,)

    @extend_schema(request=TransitionInput, responses=RegionOutput)
    def post(self, request: Request, region_id: int) -> Response:
        data = TransitionInput(data=request.data)
        data.is_valid(raise_exception=True)
        region = transition_region(cast(User, request.user), region_id, **data.validated_data)
        return Response(RegionOutput(region).data)


class ForecastView(RegionsView):
    @extend_schema(parameters=[ForecastQuery], responses=ForecastOutput)
    def get(self, request: Request, tenant_id: int, region_id: int = 0) -> Response:
        region = get_object_or_404(self.get_queryset(), pk=region_id)
        query = ForecastQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        target = query.validated_data["date"]
        today = timezone.now().astimezone(ZoneInfo(region.timezone)).date()
        if not today - timedelta(days=366) <= target <= today + timedelta(days=14):
            return Response({"code": "date_out_of_range"}, status=400)
        saved = (
            ForecastResult.objects.select_related("snapshot__job__binding")
            .filter(snapshot__job__binding__region=region, snapshot__job__target_date=target)
            .order_by("-snapshot__collected_at")
            .first()
        )
        fresh = (
            saved is not None
            and region.status == "approved"
            and (saved.snapshot.job.binding_id == region.approved_binding_id)
            and saved.snapshot.collected_at >= timezone.now() - timedelta(hours=24)
        )
        return Response(
            {
                "region_id": region.pk,
                "date": target,
                "timezone": saved.snapshot.job.binding.timezone if saved else region.timezone,
                "status": region.status,
                "freshness": "missing" if saved is None else "fresh" if fresh else "stale",
                "result": saved.payload if saved else None,
                "collected_at": saved.snapshot.collected_at if saved else None,
                "issued_at": saved.snapshot.issued_at if saved else None,
                "binding_revision": saved.snapshot.job.binding_id if saved else None,
                "scheduled_slot": saved.snapshot.job.scheduled_slot if saved else None,
                "late": saved.snapshot.collected_at > saved.snapshot.job.scheduled_slot
                if saved
                else None,
            }
        )

    http_method_names = ["get", "head", "options"]  # noqa: RUF012 -- Django class setting.
