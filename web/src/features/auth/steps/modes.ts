import type { AuthResult } from "@/lib/auth-api";

export type Mode =
    | "login"
    | "register"
    | "verify"
    | "mfa"
    | "forgot"
    | "reset"
    | "socialSignup";
export type PendingMode = Extract<
    Mode,
    "verify" | "mfa" | "reset" | "socialSignup"
>;
export type AuthMessage =
    "" | "resetDone" | "verified" | "codeRequested" | "passwordChanged";
// A stage the server no longer holds (409) or that the rate limit refuses for now (429).
export type Refusal = "ended" | "limited";

export function isPending(mode: Mode): mode is PendingMode {
    return (
        mode === "verify" ||
        mode === "mfa" ||
        mode === "reset" ||
        mode === "socialSignup"
    );
}

export function modeForPendingFlow(result: AuthResult): Mode | undefined {
    const pending = result.data?.flows?.find((flow) => flow.is_pending)?.id;
    return pending === "provider_signup"
        ? "socialSignup"
        : pending === "verify_email"
          ? "verify"
          : pending === "mfa_authenticate"
            ? "mfa"
            : pending === "password_reset_by_code"
              ? "reset"
              : undefined;
}

export function takePasswordChanged() {
    try {
        if (!window.sessionStorage.getItem("pyracast.passwordChanged"))
            return false;
        window.sessionStorage.removeItem("pyracast.passwordChanged");
        return true;
    } catch {
        return false;
    }
}
