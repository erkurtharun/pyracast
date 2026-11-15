"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { ApiError, type Session } from "@/lib/api";
import { authRequest } from "@/lib/auth-api";
import {
    ReauthenticationForm,
    refreshExpiredSession,
} from "./security-reauthentication";
import { TwoFactorSettings } from "./two-factor-settings";
import { ActionForm, Field } from "./ui";

export function AccountSecurity({
    token,
    mfaRequired,
    onSession,
}: {
    token: string;
    mfaRequired: boolean;
    onSession: (session: Session) => void;
}) {
    const t = useTranslations("Security");
    const [identityNotice, setIdentityNotice] = useState<
        "identityConfirmed" | "authenticatorConfirmed"
    >();

    return (
        <>
            <section className="panel narrow">
                <details className="account-disclosure password-disclosure">
                    <summary>
                        <h2>{t("changePassword")}</h2>
                    </summary>
                    <ActionForm
                        submit={t("changePassword")}
                        onSubmit={async (data) => {
                            const result = await authRequest(
                                "account/password/change",
                                "POST",
                                token,
                                Object.fromEntries(data),
                            );
                            if (result.status === 401) {
                                if (!result.meta?.is_authenticated) {
                                    await refreshExpiredSession(
                                        onSession,
                                        t("sessionExpired"),
                                    );
                                }
                                throw new ApiError(t("confirmIdentityFirst"));
                            }
                        }}
                    >
                        <Field
                            label={t("currentPassword")}
                            name="current_password"
                            type="password"
                            autoComplete="current-password"
                        />
                        <Field
                            label={t("newPassword")}
                            name="new_password"
                            type="password"
                            autoComplete="new-password"
                            minLength={12}
                        />
                    </ActionForm>
                </details>
            </section>
            <TwoFactorSettings
                token={token}
                mfaRequired={mfaRequired}
                onSession={onSession}
            />
            <section className="panel narrow">
                <details className="identity-details">
                    <summary>{t("identity")}</summary>
                    <div className="identity-details-body">
                        <p>{t("sensitiveChanges")}</p>
                        {identityNotice && (
                            <div className="notice" role="status">
                                {t(identityNotice)}
                            </div>
                        )}
                        <ReauthenticationForm
                            mode="password"
                            token={token}
                            onSession={onSession}
                            onVerified={() => {
                                setIdentityNotice("identityConfirmed");
                            }}
                        />
                        <ReauthenticationForm
                            mode="mfa"
                            token={token}
                            onSession={onSession}
                            onVerified={() => {
                                setIdentityNotice("authenticatorConfirmed");
                            }}
                        />
                    </div>
                </details>
            </section>
        </>
    );
}
