import { browserErrors } from "@/i18n/browser";
import { ApiError } from "@/lib/api/errors";
import type { AuthenticatorRecord } from "@/lib/auth-api";

export type RecoveryCounts = {
    unused: number;
    total: number;
};

// What the account's authenticator list says; dates are seconds since the epoch.
export type Facts = {
    addedAt?: number;
    lastUsedAt?: number | null;
    recovery?: RecoveryCounts;
};

export type TwoFactorView =
    | { kind: "loading" }
    | { kind: "loadError"; message?: string }
    | { kind: "disabled" }
    | { kind: "enrolling"; secret: string; totpUrl: string }
    | { kind: "enabled"; facts: Facts }
    | { kind: "codes"; facts: Facts; codes: string[] }
    | { kind: "confirmReplace"; facts: Facts }
    | { kind: "confirmRemove"; facts: Facts };

export type StatusNotice =
    | "authenticatorActivated"
    | "authenticatorRemoved"
    | "codesCopied"
    | "codesDownloaded"
    | "codesReplaced"
    | "recoverySavedAck"
    | "setupKeyCopied"
    | "enterFreshCode";

export function parseAuthenticators(
    records: AuthenticatorRecord[] | undefined,
): { totpEnabled: boolean; facts: Facts } {
    if (
        !Array.isArray(records) ||
        records.some((item) => !item || typeof item.type !== "string")
    ) {
        throw new ApiError(browserErrors()("invalidResponse"));
    }
    const totp = records.find((item) => item.type === "totp");
    const codes = records.find((item) => item.type === "recovery_codes");
    return {
        totpEnabled: totp !== undefined,
        facts: {
            addedAt: totp?.created_at,
            lastUsedAt: totp?.last_used_at,
            recovery: codes
                ? {
                      unused: codes.unused_code_count ?? 0,
                      total: codes.total_code_count ?? 0,
                  }
                : undefined,
        },
    };
}
