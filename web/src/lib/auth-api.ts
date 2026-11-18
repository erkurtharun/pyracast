import { ApiError, errorFromResponse } from "./api/errors";
import { exchange, type Method } from "./api/lane";
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
    method: Method,
    body?: unknown,
): Promise<AuthResult<TData>> {
    const t = browserErrors();
    const { status, ok, data } = await exchange({
        method,
        url: `/api/auth/browser/v1/${path}`,
        body,
    });
    if (!data || typeof data !== "object" || Array.isArray(data))
        throw new ApiError(
            ok ? t("invalidResponse") : t("request", { status }),
            { status },
        );
    // A 401 carries allauth's flows and the 404 means no authenticator yet: callers read both.
    if (
        !ok &&
        status !== 401 &&
        !(
            status === 404 &&
            method === "GET" &&
            path === "account/authenticators/totp"
        )
    )
        throw errorFromResponse(status, data);
    return data as AuthResult<TData>;
}
