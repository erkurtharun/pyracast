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
    body: unknown;
};

// The only errors whose message is shown as written; any other failure is shown as a localized generic message.
export class DisplayError extends Error {}

export class ApiError extends DisplayError {
    readonly status: number;
    readonly code: ErrorCode | null;
    readonly fields: Record<string, string>;
    readonly retryAfter: number | null;
    readonly requestId: string | null;
    // The whole contract body, for a refusal that carries more than its code.
    readonly body: unknown;

    constructor(message: string, details: Partial<ErrorDetails> = {}) {
        super(message);
        this.status = details.status ?? 0;
        this.code = details.code ?? null;
        this.fields = details.fields ?? {};
        this.retryAfter = details.retryAfter ?? null;
        this.requestId = details.requestId ?? null;
        this.body = details.body ?? null;
    }
}

// Status 0 like a lost connection, but this tab refused the request before it left.
export class NotSentError extends ApiError {}

export class UnconfirmedError extends ApiError {}

export function answerLost(error: unknown): boolean {
    return error instanceof UnconfirmedError;
}

// A POST that changes nothing, such as a preview, failed as a read would when its answer was lost.
export function readFailure(error: unknown) {
    if (!(error instanceof UnconfirmedError)) return error;
    const { status } = error;
    const t = browserErrors();
    const message =
        status === 0
            ? t("network")
            : status < 300
              ? t("invalidResponse")
              : t("request", { status });
    return new ApiError(message, { status });
}

// The compiler keeps this set equal to the generated vocabulary.
const ERROR_CODES: Record<ErrorCode, true> = {
    invalid_request: true,
    bad_request: true,
    session_ended: true,
    session_changed: true,
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
    conflict: true,
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
    maintenance: true,
    request_too_large: true,
    forecast_sources_changed: true,
    configuration_unavailable: true,
    location_already_has_plant: true,
    plant_submission_required: true,
    estimated_actuals_unavailable: true,
    location_unchanged: true,
    location_already_exists: true,
    calculation_out_of_range: true,
    trash_retention_expired: true,
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
    production_implausible: true,
    production_revision_changed: true,
    request_already_closed: true,
    request_conflict: true,
    review_context_changed: true,
    workspace_admin_changed: true,
    reminder_time_required: true,
    duplicate_reminder_times: true,
    too_many_reminder_times: true,
    recipient_required: true,
    duplicate_recipients: true,
    recipient_limit_exceeded: true,
    reminder_schedule_changed: true,
    reminder_disclosure_changed: true,
    plant_not_approved: true,
    provider_account_unavailable: true,
    configuration_changed: true,
    production_source_changed: true,
    outage_history_changed: true,
    incumbent_changed: true,
    calibration_not_activatable: true,
    review_reason_required: true,
    plant_parameters_changed: true,
    plant_edit_blocked: true,
};

export function displayMessage(error: unknown, fallback: string): string {
    return error instanceof DisplayError && error.message
        ? error.message
        : fallback;
}

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
            body: data,
        },
    );
}
