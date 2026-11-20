"use client";
import { useTranslations } from "next-intl";
import type { Ref } from "react";
import { ReauthenticationForm } from "../security-reauthentication";
import type { TwoFactorView } from "./model";

export function ReauthStep({
    headingRef,
    view,
    email,
    busy,
    onCancel,
    onVerified,
}: {
    headingRef: Ref<HTMLHeadingElement>;
    view: Extract<TwoFactorView, { kind: "reauth" }>;
    email: string;
    busy: boolean;
    onCancel: () => void;
    onVerified: () => Promise<void>;
}) {
    const t = useTranslations("Security");
    return (
        <div className="mfa-subpanel">
            <h3 ref={headingRef} tabIndex={-1}>
                {view.mode === "mfa"
                    ? t("reauthMfaTitle")
                    : view.mode === "email"
                      ? t("emailReauthTitle")
                      : t("reauthPasswordTitle")}
            </h3>
            {view.afterActivation ? (
                <div className="notice" role="status">
                    {t("nextUnusedCodeHint")}
                </div>
            ) : (
                <p>
                    {view.mode === "mfa"
                        ? t("reauthMfaDescription")
                        : view.mode === "email"
                          ? t("emailReauthDescription")
                          : t("reauthPasswordDescription")}
                </p>
            )}
            <ReauthenticationForm
                key={view.mode}
                mode={view.mode}
                email={email}
                autoFocus
                disabled={busy}
                onCancel={onCancel}
                onVerified={onVerified}
            />
        </div>
    );
}
