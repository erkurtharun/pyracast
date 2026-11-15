"use client";

import { useTranslations } from "next-intl";
import { QRCodeSVG } from "qrcode.react";
import { useId, type Ref } from "react";

type AuthenticatorSetupProps = {
    headingRef: Ref<HTMLHeadingElement>;
    secret: string;
    totpUrl: string;
    code: string;
    onCodeChange: (code: string) => void;
    error?: string;
    busy: boolean;
    verifying: boolean;
    onCopyKey: () => void;
    onActivate: () => void;
    onCancel: () => void;
};

export function AuthenticatorSetup({
    headingRef,
    secret,
    totpUrl,
    code,
    onCodeChange,
    error,
    busy,
    verifying,
    onCopyKey,
    onActivate,
    onCancel,
}: AuthenticatorSetupProps) {
    const t = useTranslations("Security");
    const inputId = useId();
    return (
        <div className="mfa-subpanel">
            <h3 ref={headingRef} tabIndex={-1}>
                {t("setupTitle")}
            </h3>
            <p>{t("setupStepScan")}</p>

            <div className="mfa-qr-frame">
                <QRCodeSVG
                    value={totpUrl}
                    size={208}
                    marginSize={4}
                    bgColor="#ffffff"
                    fgColor="#000000"
                    role="img"
                    aria-label={t("qrCodeLabel")}
                />
            </div>

            <details className="mfa-manual-details">
                <summary>{t("manualDetails")}</summary>
                <div className="mfa-details-body">
                    <p className="fine">{t("setupKey")}</p>
                    <div className="mfa-secret-row">
                        <code className="mfa-secret-code">{secret}</code>
                        <button
                            className="button secondary"
                            type="button"
                            disabled={busy}
                            onClick={onCopyKey}
                        >
                            {t("copySetupKey")}
                        </button>
                    </div>
                </div>
            </details>

            <form
                onSubmit={(event) => {
                    event.preventDefault();
                    onActivate();
                }}
            >
                <fieldset disabled={busy}>
                    <div className="field">
                        <label htmlFor={inputId}>
                            {t("authenticatorCode")}
                        </label>
                        <input
                            id={inputId}
                            name="code"
                            type="text"
                            inputMode="numeric"
                            autoComplete="one-time-code"
                            required
                            value={code}
                            onChange={(event) =>
                                onCodeChange(event.target.value)
                            }
                            aria-invalid={error ? true : undefined}
                            aria-describedby={
                                error ? `${inputId}-error` : `${inputId}-hint`
                            }
                        />
                        {error ? (
                            <span
                                className="field-error"
                                id={`${inputId}-error`}
                            >
                                {error}
                            </span>
                        ) : (
                            <span className="field-hint" id={`${inputId}-hint`}>
                                {t("enterSixDigitCode")}
                            </span>
                        )}
                    </div>
                    <div className="button-group">
                        <button className="button" type="submit">
                            {verifying
                                ? t("verifying")
                                : t("activateAuthenticator")}
                        </button>
                        <button
                            className="button secondary"
                            type="button"
                            onClick={onCancel}
                        >
                            {t("cancel")}
                        </button>
                    </div>
                </fieldset>
            </form>
        </div>
    );
}
