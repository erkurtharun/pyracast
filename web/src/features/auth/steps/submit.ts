import { ApiError } from "@/lib/api/errors";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { isPending, type AuthMessage, type Mode, type Refusal } from "./modes";

export type Step =
    | { mode: Mode; message: AuthMessage }
    | { result: AuthResult }
    | { refusal: Refusal };
type Label = "invalidReset" | "tooManyAttempts" | "tooManyRequests";

async function post(
    mode: Mode,
    path: string,
    payload: Record<string, unknown>,
    label: (key: Label) => string,
): Promise<{ result: AuthResult } | { refusal: Refusal }> {
    try {
        return { result: await authRequest(path, "POST", payload) };
    } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        if (isPending(mode) && error.status === 409)
            return { refusal: "ended" };
        if (isPending(mode) && error.status === 429)
            return { refusal: "limited" };
        if (error.status === 429)
            throw new ApiError(
                label(mode === "login" ? "tooManyAttempts" : "tooManyRequests"),
            );
        throw error;
    }
}

export async function submitAuth(
    mode: Mode,
    payload: Record<string, FormDataEntryValue>,
    label: (key: Label) => string,
): Promise<Step> {
    if (mode === "forgot") {
        await post(mode, "auth/password/request", payload, label);
        return { mode: "reset", message: "" };
    }
    if (mode === "reset") {
        const answer = await post(
            mode,
            "auth/password/reset",
            { key: payload.key, password: payload.password },
            label,
        );
        if ("refusal" in answer) return answer;
        const { result } = answer;
        if (
            result.status !== 200 &&
            (result.status !== 401 ||
                result.errors?.length ||
                result.data?.flows?.some(
                    (flow) =>
                        flow.id === "password_reset_by_code" && flow.is_pending,
                ))
        )
            throw new ApiError(
                result.errors?.map((error) => error.message).join(" ") ||
                    label("invalidReset"),
            );
        return { mode: "login", message: "resetDone" };
    }
    const path = {
        login: "auth/login",
        register: "auth/signup",
        socialSignup: "auth/provider/signup",
        verify: "auth/email/verify",
        mfa: "auth/2fa/authenticate",
    }[mode];
    const answer = await post(mode, path, payload, label);
    if ("refusal" in answer) return answer;
    const { result } = answer;
    if (
        mode === "verify" &&
        !result.meta?.is_authenticated &&
        !result.errors?.length &&
        !result.data?.flows?.some(
            (flow) => flow.id === "verify_email" && flow.is_pending,
        )
    )
        return { mode: "login", message: "verified" };
    return answer;
}
