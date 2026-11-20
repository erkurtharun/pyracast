"use client";
import { useTranslations } from "next-intl";
import type { RecoveryCounts } from "./model";

export function EnabledPanel({
    recovery,
    mfaRequired,
    busy,
    loadingRecovery,
    onShowRecovery,
    onReplace,
    onRemove,
}: {
    recovery?: RecoveryCounts;
    mfaRequired: boolean;
    busy: boolean;
    loadingRecovery: boolean;
    onShowRecovery: () => void;
    onReplace: () => void;
    onRemove: () => void;
}) {
    const t = useTranslations("Security");
    return (
        <div className="mfa-subpanel">
            {recovery && (
                <p>
                    {t("recoveryCount", {
                        unused: recovery.unused,
                        total: recovery.total,
                    })}
                </p>
            )}
            {recovery && recovery.unused <= 2 && (
                <div className="notice" role="status">
                    {t("recoveryLowWarning")}
                </div>
            )}
            {mfaRequired && <p className="fine">{t("policyRequiredNote")}</p>}
            <div className="button-group mfa-actions">
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onShowRecovery}
                >
                    {loadingRecovery ? t("working") : t("showRecovery")}
                </button>
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onReplace}
                >
                    {t("replaceRecovery")}
                </button>
                {!mfaRequired && (
                    <button
                        className="text-button"
                        type="button"
                        disabled={busy}
                        onClick={onRemove}
                    >
                        {t("removeAuthenticator")}
                    </button>
                )}
            </div>
        </div>
    );
}
