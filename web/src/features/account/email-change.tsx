"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api/errors";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { loadSession } from "@/lib/session-client";
import { useOneAction } from "./one-action";
import { refreshExpiredSession } from "./security-reauthentication";
import { StepUpPrompt, stepUpMode, useStepUp } from "./step-up";
import { ActionForm, AsyncButton, Field, formText } from "@/ui/form";
import { Notice } from "@/ui/ui";

type Address = { email: string; verified: boolean; primary: boolean };
type Step =
    | { kind: "loading" }
    | { kind: "idle"; notice?: "expired" | "cancelled" }
    | { kind: "codeSent"; email: string; resent?: boolean }
    | { kind: "done"; email: string };

function pendingChange(addresses: Address[] | undefined): Step {
    const pending = addresses?.find((address) => !address.verified);
    return pending
        ? { kind: "codeSent", email: pending.email }
        : { kind: "idle" };
}

// The session's code for the change has ended: the change starts again from the address.
function codeEnded(failure: unknown) {
    return (
        failure instanceof ApiError &&
        failure.status === 409 &&
        failure.code !== "session_changed"
    );
}

export function EmailChange({ email }: { email: string }) {
    const t = useTranslations("Dashboard");
    const security = useTranslations("Security");
    const [step, setStep] = useState<Step>({ kind: "loading" });
    const [error, setError] = useState<string>();
    const stepUp = useStepUp();
    const { acting, run } = useOneAction();

    useEffect(() => {
        let active = true;
        authRequest<Address[]>("account/email", "GET")
            .then((result) => {
                if (active) setStep(pendingChange(result.data));
            })
            .catch(() => {
                if (active) setStep({ kind: "idle" });
            });
        return () => {
            active = false;
        };
    }, []);

    async function confirmed(
        result: AuthResult<unknown>,
        repeat: () => Promise<void>,
    ): Promise<boolean> {
        if (result.status !== 401) return true;
        const mode = stepUpMode(result);
        if (!mode) await refreshExpiredSession(security("sessionExpired"));
        else stepUp.ask(mode, () => run(repeat));
        return false;
    }

    async function requestCode(address: string) {
        setError(undefined);
        const result = await authRequest("account/email", "POST", {
            email: address,
        });
        if (await confirmed(result, () => requestCode(address)))
            setStep({ kind: "codeSent", email: address });
    }

    async function verify(code: string, address: string) {
        setError(undefined);
        try {
            const result = await authRequest("auth/email/verify", "POST", {
                key: code,
            });
            if (!(await confirmed(result, () => verify(code, address)))) return;
        } catch (failure) {
            if (codeEnded(failure)) {
                setStep({ kind: "idle", notice: "expired" });
                return;
            }
            throw failure;
        }
        setStep({ kind: "done", email: address });
        await loadSession();
    }

    async function resend() {
        setError(undefined);
        try {
            await authRequest("auth/email/verify/resend", "POST", {});
        } catch (failure) {
            if (codeEnded(failure)) {
                setStep({ kind: "idle", notice: "expired" });
                return;
            }
            throw failure;
        }
        setStep((current) =>
            current.kind === "codeSent"
                ? { ...current, resent: true }
                : current,
        );
    }

    async function cancelChange(address: string) {
        setError(undefined);
        const result = await authRequest("account/email", "DELETE", {
            email: address,
        });
        if (await confirmed(result, () => cancelChange(address)))
            setStep({ kind: "idle", notice: "cancelled" });
    }

    return (
        <details className="account-disclosure">
            <summary>{t("changeEmail")}</summary>
            <p className="fine">{t("currentEmail", { email })}</p>
            {error && <Notice error>{error}</Notice>}
            <StepUpPrompt stepUp={stepUp} email={email} onError={setError} />
            {!stepUp.pending && step.kind === "idle" && (
                <>
                    {step.notice && (
                        <Notice>
                            {step.notice === "expired"
                                ? t("emailCodeExpired")
                                : t("emailChangeCancelled")}
                        </Notice>
                    )}
                    <ActionForm
                        submit={t("sendVerification")}
                        onSubmit={(data) =>
                            run(() => requestCode(formText(data, "email")))
                        }
                    >
                        <Field
                            label={t("newEmail")}
                            name="email"
                            type="email"
                            autoComplete="email"
                            maxLength={254}
                            hint={t("emailChangeSignsOut")}
                            required
                        />
                    </ActionForm>
                </>
            )}
            {!stepUp.pending && step.kind === "codeSent" && (
                <>
                    <Notice>
                        {t("emailCodeSentTo", { email: step.email })}
                    </Notice>
                    <fieldset disabled={acting}>
                        <ActionForm
                            submit={t("verifyEmail")}
                            onSubmit={(data) =>
                                run(() =>
                                    verify(formText(data, "key"), step.email),
                                )
                            }
                        >
                            <Field
                                label={t("emailCode")}
                                name="key"
                                autoComplete="one-time-code"
                                required
                            />
                        </ActionForm>
                    </fieldset>
                    {step.resent && <Notice>{t("emailCodeResent")}</Notice>}
                    <p className="fine">{t("emailCodeResendLimit")}</p>
                    <AsyncButton
                        variant="secondary"
                        disabled={acting}
                        onClick={() => run(resend)}
                    >
                        {t("sendCodeAgain")}
                    </AsyncButton>
                    <AsyncButton
                        variant="secondary"
                        disabled={acting}
                        onClick={() => run(() => cancelChange(step.email))}
                    >
                        {t("useAnotherAddress")}
                    </AsyncButton>
                </>
            )}
            {step.kind === "done" && (
                <Notice>{t("emailChanged", { email: step.email })}</Notice>
            )}
        </details>
    );
}
