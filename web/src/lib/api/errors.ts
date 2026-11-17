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

function isErrorBody(data: unknown): data is ErrorBody {
    return (
        !!data &&
        typeof data === "object" &&
        !Array.isArray(data) &&
        "code" in data &&
        typeof data.code === "string" &&
        "message" in data &&
        typeof data.message === "string"
    );
}

export function errorFromResponse(status: number, data: unknown): ApiError {
    const t = browserErrors();
    if (!isErrorBody(data))
        return new ApiError(t("request", { status }), { status });
    const requestId = data.request_id ?? null;
    return new ApiError(
        requestId
            ? `${data.message} ${t("reference", { reference: requestId })}`
            : data.message,
        {
            status,
            code: data.code,
            fields: data.fields ?? {},
            retryAfter: data.retry_after ?? null,
            requestId,
        },
    );
}
