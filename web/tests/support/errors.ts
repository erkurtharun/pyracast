import type { ErrorBody, ErrorCode } from "../../src/lib/api/errors";

export function errorBody(
    code: ErrorCode,
    message: string,
    details: Omit<ErrorBody, "code" | "message"> = {},
): ErrorBody {
    return { code, message, ...details };
}

type AllauthError = { message: string; param?: string; code?: string };

// The server adds the contract keys beside allauth's own keys.
export function allauthErrorBody(
    status: number,
    code: ErrorCode,
    errors: AllauthError[],
) {
    const fields: Record<string, string> = {};
    for (const error of errors) {
        if (error.param)
            fields[error.param] = [fields[error.param], error.message]
                .filter(Boolean)
                .join(" ");
    }
    return {
        status,
        errors,
        ...errorBody(
            code,
            errors[0]?.message ?? "",
            Object.keys(fields).length ? { fields } : {},
        ),
    };
}
