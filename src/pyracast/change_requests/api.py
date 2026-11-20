from collections.abc import Callable
from typing import Any, cast

from django.contrib.auth.models import User
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from pyracast.change_requests import operations
from pyracast.change_requests.models import ChangeRequest
from pyracast.change_requests.queries import (
    inbox,
    request_detail,
    request_summaries,
    request_summary,
)
from pyracast.change_requests.serializers import (
    ApprovalInput,
    ChangeRequestOutput,
    ChangeRequestSummaryOutput,
    InboxQuery,
    IncidentRequestInput,
    ProductionRequestInput,
    RejectionInput,
)
from pyracast.plants.production_changes import ProductionChange
from pyracast.plants.serializers import EmptyInput


class RequestView(GenericAPIView[ChangeRequest]):
    serializer_class = ChangeRequestOutput  # type: ignore[assignment]

    def summary(
        self,
        request: Request,
        tenant_id: int,
        act: Callable[[], ChangeRequest],
        *,
        status: int = 200,
    ) -> Response:
        with transaction.atomic():
            data = request_summary(cast(User, request.user), tenant_id, act())
        return Response(ChangeRequestSummaryOutput(data).data, status=status)


class ProductionRequestsView(RequestView):
    @extend_schema(request=ProductionRequestInput, responses={201: ChangeRequestSummaryOutput})
    def post(self, request: Request, tenant_id: int, region_id: int, plant_id: int) -> Response:
        data = ProductionRequestInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data.copy()
        values["changes"] = [ProductionChange(**row) for row in values["changes"]]
        return self.summary(
            request,
            tenant_id,
            lambda: operations.submit_production(
                cast(User, request.user), tenant_id, region_id, plant_id, **values
            ),
            status=201,
        )


class IncidentRequestsView(RequestView):
    @extend_schema(request=IncidentRequestInput, responses={201: ChangeRequestSummaryOutput})
    def post(self, request: Request, tenant_id: int, region_id: int, plant_id: int) -> Response:
        data = IncidentRequestInput(data=request.data)
        data.is_valid(raise_exception=True)
        return self.summary(
            request,
            tenant_id,
            lambda: operations.submit_incident(
                cast(User, request.user), tenant_id, region_id, plant_id, **data.validated_data
            ),
            status=201,
        )


class InboxView(RequestView):
    @extend_schema(parameters=[InboxQuery], responses=ChangeRequestSummaryOutput(many=True))
    def get(self, request: Request, tenant_id: int) -> Response:
        filters = request.query_params.copy()
        filters.pop("page", None)
        query = InboxQuery(data=filters)
        query.is_valid(raise_exception=True)
        actor = cast(User, request.user)
        page = self.paginate_queryset(inbox(actor, tenant_id, **query.validated_data))
        rows = request_summaries(actor, tenant_id, list(page or []))
        return self.get_paginated_response(
            ChangeRequestSummaryOutput(cast(Any, rows), many=True).data
        )


class DetailView(RequestView):
    @extend_schema(responses=ChangeRequestOutput)
    def get(self, request: Request, tenant_id: int, request_id: int) -> Response:
        return Response(
            ChangeRequestOutput(
                request_detail(cast(User, request.user), tenant_id, request_id)
            ).data
        )


class ApproveView(RequestView):
    @extend_schema(request=ApprovalInput, responses=ChangeRequestSummaryOutput)
    def post(self, request: Request, tenant_id: int, request_id: int) -> Response:
        data = ApprovalInput(data=request.data)
        data.is_valid(raise_exception=True)
        return self.summary(
            request,
            tenant_id,
            lambda: operations.approve_request(
                cast(User, request.user), tenant_id, request_id, **data.validated_data
            ),
        )


class RejectView(RequestView):
    @extend_schema(request=RejectionInput, responses=ChangeRequestSummaryOutput)
    def post(self, request: Request, tenant_id: int, request_id: int) -> Response:
        data = RejectionInput(data=request.data)
        data.is_valid(raise_exception=True)
        return self.summary(
            request,
            tenant_id,
            lambda: operations.reject_request(
                cast(User, request.user), tenant_id, request_id, **data.validated_data
            ),
        )


class WithdrawView(RequestView):
    @extend_schema(request=EmptyInput, responses=ChangeRequestSummaryOutput)
    def post(self, request: Request, tenant_id: int, request_id: int) -> Response:
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        return self.summary(
            request,
            tenant_id,
            lambda: operations.withdraw_request(cast(User, request.user), tenant_id, request_id),
        )


class SeenView(RequestView):
    @extend_schema(request=EmptyInput, responses=ChangeRequestSummaryOutput)
    def post(self, request: Request, tenant_id: int, request_id: int) -> Response:
        data = EmptyInput(data=request.data)
        data.is_valid(raise_exception=True)
        return self.summary(
            request,
            tenant_id,
            lambda: operations.mark_seen(cast(User, request.user), tenant_id, request_id),
        )
