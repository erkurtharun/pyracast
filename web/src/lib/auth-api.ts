import { ApiError, errorFromResponse } from "./api/errors";
import { browserErrors } from "@/i18n/browser";

export type AuthData = {
    flows?: { id: string; is_pending?: boolean }[];
    unused_codes?: string[];
    unused_code_count?: number;
    total_code_count?: number;
    [key: string]: unknown;
};

export type AuthenticatorRecord = {
    type: string;
    unused_code_count?: number;
    total_code_count?: number;
    created_at?: number;
    last_used_at?: number | null;
};

export type AuthResult<TData = AuthData> = {
    status: number;
    data?: TData;
    meta?: {
        is_authenticated?: boolean;
        secret?: string;
        totp_url?: string;
        password_changed?: boolean;
    };
    errors?: { message: string; param?: string; code?: string }[];
};

export async function authRequest<TData = AuthData>(
    path: string,
    method: string,
    token: string,
    body?: unknown,
): Promise<AuthResult<TData>> {
    const t = browserErrors();
    const response = await fetch(`/api/auth/browser/v1/${path}`, {
        method,
        credentials: "same-origin",
        cache: "no-store",
        headers: {
            "X-CSRFToken": token,
            ...(body === undefined
                ? {}
                : { "Content-Type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data: unknown = await response.json().catch(() => null);
    if (!data || typeof data !== "object" || Array.isArray(data))
        throw new ApiError(
            response.ok
                ? t("invalidResponse")
                : t("request", { status: response.status }),
            { status: response.status },
        );
    const result = data as AuthResult<TData>;
    if (
        !response.ok &&
        response.status !== 401 &&
        !(
            response.status === 404 &&
            method === "GET" &&
            path === "account/authenticators/totp"
        )
    ) {
        const errors = result.errors ?? [];
        throw new ApiError(
            errors.map((error) => error.message).join(" ") ||
                errorFromResponse(response.status, data).message,
            {
                status: response.status,
                fields: Object.fromEntries(
                    errors
                        .filter((error) => error.param)
                        .map((error) => [error.param!, error.message]),
                ),
            },
        );
    }
    return result;
}
