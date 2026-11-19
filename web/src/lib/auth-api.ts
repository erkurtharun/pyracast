import { ApiError, errorFromResponse } from "./api/errors";
import {
    exchange,
    sessionStillHeld,
    type Exchange,
    type Method,
} from "./api/lane";
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

function signedOut({ status, data }: Exchange) {
    return (
        status === 401 && !(data as AuthResult | null)?.meta?.is_authenticated
    );
}

export async function authRequest<TData = AuthData>(
    path: string,
    method: Method,
    body?: unknown,
): Promise<AuthResult<TData>> {
    const t = browserErrors();
    const request = { method, url: `/api/auth/browser/v1/${path}`, body };
    let answer = await exchange(request);
    // A read carrying a key that a reauthentication replaced meanwhile is answered as signed out;
    // it is repeated once when a session read sent after that answer still shows the session.
    if (method === "GET" && signedOut(answer) && (await sessionStillHeld()))
        answer = await exchange(request);
    const { status, ok, data } = answer;
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
