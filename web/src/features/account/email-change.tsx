"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { ApiError, DisplayError } from "@/lib/api/errors";
import { authRequest } from "@/lib/auth-api";
import { loadSession } from "@/lib/session-client";
import {
    getReauthMode,
    ReauthenticationForm,
    refreshExpiredSession,
    type ReauthMode,
} from "./security-reauthentication";
import { ActionForm, Field } from "@/ui/form";
import { Notice } from "@/ui/ui";

type Step =
    | { status: "address" | "sent"; address: string }
    | { status: "reauthenticate"; mode: ReauthMode; address: string };

export function EmailChange({
    email,
    hasPassword,
}: {
    email: string;
    hasPassword: boolean;
}) {
    const t = useTranslations("Dashboard");
    const security = useTranslations("Security");
    const auth = useTranslations("Auth");
    const [step, setStep] = useState<Step>({ status: "address", address: "" });

    async function requestCode(address: string, replay = false) {
        const result = await authRequest("account/email", "POST", {
            email: address,
        });
        if (result.status !== 401) {
            setStep({ status: "sent", address });
            return;
        }
        if (!result.meta?.is_authenticated)
            await refreshExpiredSession(t("sessionExpired"));
        if (replay) throw new ApiError(security("reauthStillRequired"));
        setStep({
            status: "reauthenticate",
            mode: getReauthMode(result, hasPassword),
            address,
        });
    }

    return (
        <details className="account-disclosure">
            <summary>{t("changeEmail")}</summary>
            <p className="fine">{t("currentEmail", { email })}</p>
            {step.status === "sent" && <Notice>{auth("codeSent")}</Notice>}
            {step.status === "reauthenticate" ? (
                <ReauthenticationForm
                    mode={step.mode}
                    email={email}
                    autoFocus
                    onCancel={() =>
                        setStep({ status: "address", address: step.address })
                    }
                    onVerified={() => requestCode(step.address, true)}
                />
            ) : (
                <ActionForm
                    submit={t("sendVerification")}
                    onSubmit={(data) =>
                        requestCode(String(data.get("email") ?? ""))
                    }
                >
                    <Field
                        label={t("newEmail")}
                        name="email"
                        type="email"
                        defaultValue={step.address}
                        required
                    />
                </ActionForm>
            )}
            <ActionForm
                submit={t("verifyEmail")}
                onSubmit={async (data) => {
                    const result = await authRequest(
                        "auth/email/verify",
                        "POST",
                        { key: data.get("key") },
                    );
                    if (result.status === 401) {
                        if (!result.meta?.is_authenticated) {
                            await loadSession();
                            throw new DisplayError(t("sessionExpired"));
                        }
                        throw new DisplayError(t("confirmIdentity"));
                    }
                    await loadSession();
                }}
            >
                <Field
                    label={t("emailCode")}
                    name="key"
                    autoComplete="one-time-code"
                    required
                />
            </ActionForm>
        </details>
    );
}
