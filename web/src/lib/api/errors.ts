import type { components } from "../schema";
import { browserErrors } from "@/i18n/browser";

export type ErrorCode = components["schemas"]["ErrorCode"];
export type ErrorBody = components["schemas"]["Error"];

type ErrorDetails = {
    status: number;
    code: ErrorCode | null;
    fields: Record<string, string>;
    retryAfter: number | null;
    requestId: string | null;
};

export class ApiError extends Error {
    readonly status: number;
    readonly code: ErrorCode | null;
    readonly fields: Record<string, string>;
    readonly retryAfter: number | null;
    readonly requestId: string | null;

    constructor(message: string, details: Partial<ErrorDetails> = {}) {
        super(message);
        this.status = details.status ?? 0;
        this.code = details.code ?? null;
        this.fields = details.fields ?? {};
        this.retryAfter = details.retryAfter ?? null;
        this.requestId = details.requestId ?? null;
    }
}

// The compiler keeps this set equal to the generated vocabulary.
const ERROR_CODES: Record<ErrorCode, true> = {
    invalid_request: true,
    bad_request: true,
    session_ended: true,
    not_authenticated: true,
    csrf_failed: true,
    permission_denied: true,
    invalid_proxy_headers: true,
    membership_required: true,
    workspace_admin_required: true,
    admin_edits_directly: true,
    platform_admin_required: true,
    invitation_email_unverified: true,
    not_found: true,
    method_not_allowed: true,
    not_acceptable: true,
    unsupported_media_type: true,
    throttled: true,
    session_busy: true,
    invitation_rate_limited: true,
    provider_throttled: true,
    quota_exhausted: true,
    server_error: true,
    provider_unavailable: true,
    busy: true,
    database_conflict: true,
    request_timeout: true,
    provider_authentication_failed: true,
    forecast_sources_changed: true,
    configuration_unavailable: true,
    location_already_has_plant: true,
    plant_submission_required: true,
    estimated_actuals_unavailable: true,
    location_unchanged: true,
    location_already_exists: true,
    calculation_out_of_range: true,
    trash_retention_expired: true,
    parent_region_trashed: true,
    date_out_of_range: true,
    completed_day_required: true,
    invalid_epias_plant: true,
    epias_plant_identity_mismatch: true,
    incident_closed: true,
    incident_overlap: true,
    incident_starts_in_future: true,
    incident_ends_before_start: true,
    incident_ends_in_future: true,
    incident_capacity_exceeded: true,
    invitation_unavailable: true,
    invitation_member_exists: true,
    workspace_admin_not_removable: true,
    member_unavailable: true,
    region_revision_changed: true,
    location_change_already_pending: true,
    location_change_already_reviewed: true,
    incident_revision_changed: true,
    idempotency_key_reused: true,
    pending_request_limit: true,
    production_revision_changed: true,
    request_already_closed: true,
    request_conflict: true,
    review_context_changed: true,
    workspace_admin_changed: true,
};

function isRecord(value: unknown): value is Record<string, unknown> {
    return !!value && typeof value === "object" && !Array.isArray(value);
}

function errorCode(value: unknown): ErrorCode | null {
    return typeof value === "string" && Object.hasOwn(ERROR_CODES, value)
        ? (value as ErrorCode)
        : null;
}

function fieldMessages(value: unknown): Record<string, string> {
    if (!isRecord(value)) return {};
    return Object.fromEntries(
        Object.entries(value).filter(
            (entry): entry is [string, string] => typeof entry[1] === "string",
        ),
    );
}

function seconds(value: unknown): number | null {
    return Number.isInteger(value) && (value as number) >= 0
        ? (value as number)
        : null;
}

type ContractBody = Record<string, unknown> & {
    code: string;
    message: string;
};

export function isContractBody(data: unknown): data is ContractBody {
    return (
        isRecord(data) &&
        typeof data.code === "string" &&
        typeof data.message === "string"
    );
}

export function errorFromResponse(status: number, data: unknown): ApiError {
    const t = browserErrors();
    if (!isContractBody(data))
        return new ApiError(t("request", { status }), { status });
    const requestId =
        typeof data.request_id === "string" ? data.request_id : null;
    return new ApiError(
        requestId
            ? `${data.message} ${t("reference", { reference: requestId })}`
            : data.message,
        {
            status,
            code: errorCode(data.code),
            fields: fieldMessages(data.fields),
            retryAfter: seconds(data.retry_after),
            requestId,
        },
    );
}
