import { ApiError } from "@/lib/api/errors";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import type { AuthMessage, Mode } from "./modes";

export type Step =
    { mode: Mode; message: AuthMessage } | { result: AuthResult };
type Label = "invalidReset" | "tooManyAttempts" | "tooManyRequests";

export async function submitAuth(
    mode: Mode,
    payload: Record<string, FormDataEntryValue>,
    label: (key: Label) => string,
): Promise<Step> {
    if (mode === "forgot") {
        await authRequest("auth/password/request", "POST", payload);
        return { mode: "reset", message: "resetSent" };
    }
    if (mode === "reset") {
        const result = await authRequest("auth/password/reset", "POST", {
            key: payload.key,
            password: payload.password,
        });
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
    const result = await authRequest(path, "POST", payload).catch(
        (error: unknown) => {
            if (error instanceof ApiError && error.status === 429)
                throw new ApiError(
                    label(
                        mode === "login"
                            ? "tooManyAttempts"
                            : "tooManyRequests",
                    ),
                );
            throw error;
        },
    );
    if (
        mode === "verify" &&
        !result.meta?.is_authenticated &&
        !result.errors?.length &&
        !result.data?.flows?.some(
            (flow) => flow.id === "verify_email" && flow.is_pending,
        )
    )
        return { mode: "login", message: "verified" };
    return { result };
}
