"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { loadSession } from "@/lib/session-client";
import { ApiError } from "@/lib/api/errors";
import { authRequest } from "@/lib/auth-api";
import {
    getReauthMode,
    ReauthenticationForm,
    type ReauthMode,
    refreshExpiredSession,
} from "./security-reauthentication";
import { LinkedAccounts } from "./linked-accounts";
import { TwoFactorSettings } from "./two-factor-settings";
import { ActionForm, Field } from "@/ui/form";

export function AccountSecurity({
    mfaRequired,
    hasPassword,
    email,
}: {
    mfaRequired: boolean;
    hasPassword: boolean;
    email: string;
}) {
    const t = useTranslations("Security");
    const [pendingPassword, setPendingPassword] = useState<{
        mode: ReauthMode;
        payload: Record<string, FormDataEntryValue>;
    }>();
    async function changePassword(
        payload: Record<string, FormDataEntryValue>,
        replay = false,
    ) {
        const result = await authRequest(
            "account/password/change",
            "POST",
            payload,
        );
        if (result.status === 401) {
            if (!result.meta?.is_authenticated) {
                if (result.meta?.password_changed) {
                    setPendingPassword(undefined);
                    try {
                        window.sessionStorage.setItem(
                            "pyracast.passwordChanged",
                            "1",
                        );
                    } catch {
                        // Browser privacy settings can disable session storage.
                    }
                    await loadSession();
                    return;
                }
                await refreshExpiredSession(t("sessionExpired"));
            }
            if (replay) throw new ApiError(t("reauthStillRequired"));
            setPendingPassword({
                mode: getReauthMode(result, hasPassword),
                payload,
            });
            return;
        }
        setPendingPassword(undefined);
        await loadSession();
    }

    return (
        <>
            <section className="panel narrow">
                <details className="account-disclosure password-disclosure">
                    <summary>
                        <h2>
                            {t(hasPassword ? "changePassword" : "setPassword")}
                        </h2>
                    </summary>
                    {pendingPassword ? (
                        <ReauthenticationForm
                            mode={pendingPassword.mode}
                            email={email}
                            autoFocus
                            onCancel={() => setPendingPassword(undefined)}
                            onVerified={() =>
                                changePassword(pendingPassword.payload, true)
                            }
                        />
                    ) : (
                        <ActionForm
                            submit={t(
                                hasPassword ? "changePassword" : "setPassword",
                            )}
                            onSubmit={async (data) =>
                                changePassword(Object.fromEntries(data))
                            }
                        >
                            {hasPassword && (
                                <Field
                                    label={t("currentPassword")}
                                    name="current_password"
                                    type="password"
                                    autoComplete="current-password"
                                    required
                                />
                            )}
                            <Field
                                label={t("newPassword")}
                                name="new_password"
                                type="password"
                                autoComplete="new-password"
                                minLength={15}
                                required
                            />
                        </ActionForm>
                    )}
                </details>
            </section>
            <TwoFactorSettings
                mfaRequired={mfaRequired}
                hasPassword={hasPassword}
                email={email}
            />
            <LinkedAccounts email={email} hasPassword={hasPassword} />
        </>
    );
}
