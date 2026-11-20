import type { AuthResult } from "@/lib/auth-api";

export type Mode =
    | "login"
    | "register"
    | "verify"
    | "mfa"
    | "forgot"
    | "reset"
    | "socialSignup";
export type AuthMessage =
    | ""
    | "codeSent"
    | "resetSent"
    | "resetDone"
    | "verified"
    | "codeRequested"
    | "passwordChanged";

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
