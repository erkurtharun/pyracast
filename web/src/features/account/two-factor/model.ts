import { browserErrors } from "@/i18n/browser";
import { ApiError } from "@/lib/api/errors";
import type { AuthenticatorRecord } from "@/lib/auth-api";
import type { ReauthMode } from "../security-reauthentication";

export type RecoveryCounts = {
    unused: number;
    total: number;
};

export type PendingOperation =
    | { type: "startSetup" }
    | { type: "activateTotp"; code: string }
    | { type: "getRecovery"; afterActivation: boolean }
    | { type: "replaceRecovery" }
    | { type: "removeTotp" };

export type TwoFactorView =
    | { kind: "loading" }
    | {
          kind: "loadError";
          reason?: "sessionExpired" | "identityRequired";
          message?: string;
      }
    | { kind: "disabled" }
    | { kind: "enrolling"; secret: string; totpUrl: string }
    | { kind: "enabled"; recovery?: RecoveryCounts }
    | {
          kind: "recoverySaving";
          recovery?: RecoveryCounts;
          codes: string[];
      }
    | { kind: "recoveryViewed"; recovery?: RecoveryCounts }
    | { kind: "confirmReplace"; recovery?: RecoveryCounts }
    | { kind: "confirmRemove"; recovery?: RecoveryCounts }
    | {
          kind: "reauth";
          mode: ReauthMode;
          operation: PendingOperation;
          returnView: TwoFactorView;
          afterActivation: boolean;
      };

export type StatusNotice =
    | "authenticatorActivated"
    | "authenticatorRemoved"
    | "codesCopied"
    | "codesDownloaded"
    | "codesReplaced"
    | "recoverySavedAck"
    | "setupKeyCopied";

export type LocalFeedback = {
    text: string;
    locale: string;
};

export function parseAuthenticators(
    records: AuthenticatorRecord[] | undefined,
): {
    totpEnabled: boolean;
    recovery?: RecoveryCounts;
} {
    if (
        !Array.isArray(records) ||
        records.some((item) => !item || typeof item.type !== "string")
    ) {
        throw new ApiError(browserErrors()("invalidResponse"));
    }
    const list = records;
    const totpEnabled = list.some((item) => item.type === "totp");
    const recoveryItem = list.find((item) => item.type === "recovery_codes");
    const recovery = recoveryItem
        ? {
              unused: recoveryItem.unused_code_count ?? 0,
              total: recoveryItem.total_code_count ?? 0,
          }
        : undefined;
    return { totpEnabled, recovery };
}
