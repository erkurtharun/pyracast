import type { ErrorBody, ErrorCode } from "../../src/lib/api/errors";

export function errorBody(
    code: ErrorCode,
    message: string,
    details: Omit<ErrorBody, "code" | "message"> = {},
): ErrorBody {
    return { code, message, ...details };
}
