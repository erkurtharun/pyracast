"use client";

import { useTranslations } from "next-intl";
import type { Ref } from "react";

type RecoveryCodesProps = {
    headingRef: Ref<HTMLHeadingElement>;
    codes: string[];
    busy: boolean;
    onCopy: () => void;
    onDownload: () => void;
    onAcknowledge: () => void;
};

export function RecoveryCodes({
    headingRef,
    codes,
    busy,
    onCopy,
    onDownload,
    onAcknowledge,
}: RecoveryCodesProps) {
    const t = useTranslations("Security");
    return (
        <div className="mfa-subpanel">
            <h3 ref={headingRef} tabIndex={-1}>
                {t("saveRecoveryTitle")}
            </h3>
            <div className="notice" role="status">
                {t("recoveryShowOnceWarning")}
            </div>
            <ul className="mfa-recovery-grid">
                {codes.map((code) => (
                    <li key={code}>
                        <code>{code}</code>
                    </li>
                ))}
            </ul>
            <div className="button-group">
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onCopy}
                >
                    {t("copyCodes")}
                </button>
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onDownload}
                >
                    {t("downloadCodes")}
                </button>
            </div>
            <div className="button-group">
                <button
                    className="button"
                    type="button"
                    disabled={busy}
                    onClick={onAcknowledge}
                >
                    {t("acknowledgeSavedLabel")}
                </button>
            </div>
        </div>
    );
}
