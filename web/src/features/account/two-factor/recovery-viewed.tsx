"use client";
import { useTranslations } from "next-intl";
import type { Ref } from "react";
import type { RecoveryCounts } from "./model";

export function RecoveryViewed({
    headingRef,
    recovery,
    busy,
    onReplace,
    onBack,
}: {
    headingRef: Ref<HTMLHeadingElement>;
    recovery?: RecoveryCounts;
    busy: boolean;
    onReplace: () => void;
    onBack: () => void;
}) {
    const t = useTranslations("Security");
    return (
        <div className="mfa-subpanel">
            <h3 ref={headingRef} tabIndex={-1}>
                {t("recoveryCodesTitle")}
            </h3>
            <p>{t("recoveryAlreadyViewed")}</p>
            {recovery && (
                <p className="fine">
                    {t("recoveryCount", {
                        unused: recovery.unused,
                        total: recovery.total,
                    })}
                </p>
            )}
            <div className="button-group mfa-actions">
                <button
                    className="button"
                    type="button"
                    disabled={busy}
                    onClick={onReplace}
                >
                    {t("replaceRecovery")}
                </button>
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onBack}
                >
                    {t("back")}
                </button>
            </div>
        </div>
    );
}
