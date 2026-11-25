"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { loadSession } from "@/lib/session-client";
import { authRequest } from "@/lib/auth-api";
import { refreshExpiredSession } from "./security-reauthentication";
import { LinkedAccounts } from "./linked-accounts";
import { SignedInDevices } from "./signed-in-devices";
import { StepUpPrompt, stepUpMode, useStepUp } from "./step-up";
import { TwoFactorSettings } from "./two-factor-settings";
import { ActionForm, CheckboxField, Field, type Submitted } from "@/ui/form";
import { Notice } from "@/ui/ui";

// The server's password rules: at least 15 characters, at most 1,024.
const MIN_PASSWORD = 15;
const MAX_PASSWORD = 1024;

function rememberPasswordChange() {
    try {
        window.sessionStorage.setItem("pyracast.passwordChanged", "1");
    } catch {
        // Browser privacy settings can disable session storage.
    }
}

function PasswordChange({
    hasPassword,
    email,
}: {
    hasPassword: boolean;
    email: string;
}) {
    const t = useTranslations("Security");
    const stepUp = useStepUp();
    const [error, setError] = useState<string>();
    const [shown, setShown] = useState(false);

    async function changePassword(
        payload: Record<string, FormDataEntryValue>,
    ): Promise<Submitted> {
        setError(undefined);
        const result = await authRequest(
            "account/password/change",
            "POST",
            payload,
        );
        if (result.status === 401 && !result.meta?.password_changed) {
            const mode = stepUpMode(result);
            if (!mode) await refreshExpiredSession(t("sessionExpired"));
            else
                stepUp.ask(mode, async () => {
                    const follow = await changePassword(payload);
                    await follow?.();
                });
            return;
        }
        if (result.status === 401) rememberPasswordChange();
        return loadSession;
    }

    return (
        <section className="panel narrow">
            <details className="account-disclosure password-disclosure">
                <summary>
                    <h2>{t(hasPassword ? "changePassword" : "setPassword")}</h2>
                </summary>
                {error && <Notice error>{error}</Notice>}
                <StepUpPrompt
                    stepUp={stepUp}
                    email={email}
                    onError={setError}
                />
                {!stepUp.pending && (
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
                                type={shown ? "text" : "password"}
                                autoComplete="current-password"
                                maxLength={MAX_PASSWORD}
                                hint={t("forgotCurrentPassword")}
                                required
                            />
                        )}
                        <Field
                            label={t("newPassword")}
                            name="new_password"
                            type={shown ? "text" : "password"}
                            autoComplete="new-password"
                            minLength={MIN_PASSWORD}
                            maxLength={MAX_PASSWORD}
                            hint={t("passwordHint", { minimum: MIN_PASSWORD })}
                            required
                        />
                        <CheckboxField
                            label={t("showPasswords")}
                            checked={shown}
                            onChange={setShown}
                        />
                    </ActionForm>
                )}
            </details>
        </section>
    );
}

export function AccountSecurity({
    mfaRequired,
    hasPassword,
    email,
}: {
    mfaRequired: boolean;
    hasPassword: boolean;
    email: string;
}) {
    return (
        <>
            <PasswordChange hasPassword={hasPassword} email={email} />
            <TwoFactorSettings mfaRequired={mfaRequired} email={email} />
            <LinkedAccounts email={email} hasPassword={hasPassword} />
            <SignedInDevices />
        </>
    );
}
