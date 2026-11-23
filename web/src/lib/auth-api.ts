import { ApiError, errorFromResponse } from "./api/errors";
import { prepared, type Method } from "./api/lane";
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
        is_authenticated?: boolean | null;
        secret?: string;
        totp_url?: string;
        password_changed?: boolean;
        recovery_codes?: string[];
    };
    errors?: { message: string; param?: string; code?: string }[];
};

export function authUrl(path: string) {
    return `/api/auth/browser/v1/${path}`;
}

// django-allauth headless writes every 401 with that status in the body and meta.is_authenticated, null at a sign-in stage.
function allauthUnauthorized(status: number, data: Record<string, unknown>) {
    const { meta } = data;
    return (
        status === 401 &&
        data.status === 401 &&
        typeof meta === "object" &&
        meta !== null &&
        "is_authenticated" in meta &&
        (typeof meta.is_authenticated === "boolean" ||
            meta.is_authenticated === null)
    );
}

export async function authRequest<TData = AuthData>(
    path: string,
    method: Method,
    body?: unknown,
): Promise<AuthResult<TData>> {
    const t = browserErrors();
    const { status, ok, data } = await prepared({
        method,
        url: authUrl(path),
        body,
    });
    if (!data || typeof data !== "object" || Array.isArray(data))
        throw new ApiError(
            ok ? t("invalidResponse") : t("request", { status }),
            { status },
        );
    // Callers read allauth's 401 and the 404 that means no authenticator yet.
    if (
        !ok &&
        !allauthUnauthorized(status, data as Record<string, unknown>) &&
        !(
            status === 404 &&
            method === "GET" &&
            path === "account/authenticators/totp"
        )
    )
        throw errorFromResponse(status, data);
    return { ...(data as AuthResult<TData>), status };
}
