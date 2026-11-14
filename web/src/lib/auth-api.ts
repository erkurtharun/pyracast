import { ApiError } from "./api";
import { browserErrors } from "@/i18n/browser";

export type AuthResult = {
    status: number;
    data?: {
        flows?: { id: string; is_pending?: boolean }[];
        [key: string]: unknown;
    };
    meta?: { is_authenticated?: boolean; secret?: string; totp_url?: string };
    errors?: { message: string; param?: string; code?: string }[];
};

export async function authRequest(
    path: string,
    method: string,
    token: string,
    body?: unknown,
): Promise<AuthResult> {
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
        );
    const result = data as AuthResult;
    if (
        !response.ok &&
        response.status !== 401 &&
        !(response.status === 404 && path === "account/authenticators/totp")
    ) {
        const errors = result.errors ?? [];
        throw new ApiError(
            errors.map((error) => error.message).join(" ") ||
                t("request", { status: response.status }),
            Object.fromEntries(
                errors
                    .filter((error) => error.param)
                    .map((error) => [error.param!, error.message]),
            ),
        );
    }
    return result;
}
