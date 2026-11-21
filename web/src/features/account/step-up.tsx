"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo, useRef, useState } from "react";
import { sessionTag, type Tag } from "@/lib/api/lane";
import { displayMessage } from "@/lib/api/errors";
import type { AuthResult } from "@/lib/auth-api";
import { Notice } from "@/ui/ui";
import {
    ReauthenticationForm,
    type ReauthMode,
} from "./security-reauthentication";

const MODES: Record<string, ReauthMode> = {
    mfa_reauthenticate: "mfa",
    reauthenticate: "password",
    reauthenticate_by_code: "email",
};

// A signed-in refusal names one flow: the strongest factor of the account, which a confirmation
// must use.
export function stepUpMode(
    result: AuthResult<unknown>,
): ReauthMode | undefined {
    if (result.status !== 401 || !result.meta?.is_authenticated)
        return undefined;
    const flows = (result.data as { flows?: { id?: unknown }[] } | undefined)
        ?.flows;
    const id = flows?.[0]?.id;
    return typeof id === "string" ? MODES[id] : undefined;
}

type Pending = {
    mode: ReauthMode;
    resume: () => Promise<void>;
    tag: Tag;
    again: boolean;
};

export type StepUp = {
    pending?: Pending;
    ask: (mode: ReauthMode, resume: () => Promise<void>) => void;
    cancel: () => void;
    proceed: (confirmed: Pending) => Promise<void>;
};

// One action at a time asks for a confirmation; after the proof only that action runs, once, in
// this tab and for the session that asked. An action that asks again while it resumes is told so.
export function useStepUp(): StepUp {
    const [pending, setPending] = useState<Pending>();
    const resuming = useRef(false);
    const ask = useCallback((mode: ReauthMode, resume: () => Promise<void>) => {
        const asked = {
            mode,
            resume,
            tag: sessionTag(),
            again: resuming.current,
        };
        setPending((current) => current ?? asked);
    }, []);
    const cancel = useCallback(() => setPending(undefined), []);
    const proceed = useCallback(async (confirmed: Pending) => {
        setPending(undefined);
        resuming.current = true;
        try {
            await confirmed.resume();
        } finally {
            resuming.current = false;
        }
    }, []);
    return useMemo(
        () => ({ pending, ask, cancel, proceed }),
        [pending, ask, cancel, proceed],
    );
}

export function StepUpPrompt({
    stepUp,
    email,
    onError,
}: {
    stepUp: StepUp;
    email: string;
    onError: (message: string) => void;
}) {
    const t = useTranslations("Security");
    const errors = useTranslations("Errors");
    const { pending } = stepUp;
    if (!pending) return null;
    return (
        <div className="mfa-subpanel step-up">
            <h3>
                {pending.mode === "mfa"
                    ? t("reauthMfaTitle")
                    : pending.mode === "email"
                      ? t("emailReauthTitle")
                      : t("reauthPasswordTitle")}
            </h3>
            <p>
                {pending.mode === "mfa"
                    ? t("reauthMfaDescription")
                    : pending.mode === "email"
                      ? t("emailReauthDescription")
                      : t("reauthPasswordDescription")}
            </p>
            {pending.again && <Notice>{t("confirmAgain")}</Notice>}
            <ReauthenticationForm
                key={pending.mode}
                mode={pending.mode}
                email={email}
                autoFocus
                onCancel={stepUp.cancel}
                onVerified={async () => {
                    if (sessionTag() !== pending.tag) {
                        stepUp.cancel();
                        onError(errors("sessionChanged"));
                        return;
                    }
                    try {
                        await stepUp.proceed(pending);
                    } catch (error) {
                        onError(
                            displayMessage(error, t("reauthStillRequired")),
                        );
                    }
                }}
            />
        </div>
    );
}
