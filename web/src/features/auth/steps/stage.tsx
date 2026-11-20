"use client";
import { useTranslations } from "next-intl";
import { AsyncButton } from "@/ui/form";
import type { Mode, PendingMode, Refusal } from "./modes";

export function StageGuide({ mode, address }: { mode: Mode; address: string }) {
    const t = useTranslations("Auth.guide");
    const social = useTranslations("SocialAuth");
    const text = {
        login: null,
        register: null,
        forgot: t("forgot"),
        verify: address ? t("verify", { address }) : t("verifyUnknown"),
        reset: address ? t("reset", { address }) : t("resetUnknown"),
        mfa: t("mfa"),
        socialSignup: social("signupHint"),
    }[mode];
    return text ? <p>{text}</p> : null;
}

export function LeaveStage({
    mode,
    onLeave,
}: {
    mode: PendingMode;
    onLeave: () => Promise<void>;
}) {
    const t = useTranslations("Auth");
    return (
        <AsyncButton
            permitted
            variant="secondary"
            errorTitle={t("leaveFailed")}
            onClick={onLeave}
        >
            {t(`leave.${mode}`)}
        </AsyncButton>
    );
}

export function RefusalStage({
    refusal,
    mode,
    onRetry,
    onLeave,
}: {
    refusal: Refusal;
    mode: PendingMode;
    onRetry: () => void;
    onLeave: () => Promise<void>;
}) {
    const t = useTranslations("Auth");
    if (refusal === "ended")
        return (
            <>
                <p>
                    {mode === "verify" || mode === "reset"
                        ? t("ended.code")
                        : t("ended.step")}
                </p>
                <AsyncButton
                    permitted
                    errorTitle={t("leaveFailed")}
                    onClick={onLeave}
                >
                    {t("ended.action")}
                </AsyncButton>
            </>
        );
    return (
        <>
            <p>{t("limited.text")}</p>
            <div className="form-footer">
                <button className="button" type="button" onClick={onRetry}>
                    {t("limited.retry")}
                </button>
            </div>
            <LeaveStage mode={mode} onLeave={onLeave} />
        </>
    );
}
