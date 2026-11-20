"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";
import { sessionTag, type Tag } from "@/lib/api/lane";
import { displayMessage } from "@/lib/api/errors";
import type { AuthResult } from "@/lib/auth-api";
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

type Pending = { mode: ReauthMode; resume: () => Promise<void>; tag: Tag };

export type StepUp = {
    pending?: Pending;
    ask: (mode: ReauthMode, resume: () => Promise<void>) => void;
    cancel: () => void;
};

// One action at a time asks for a confirmation; after the proof only that action runs, once, in
// this tab and for the session that asked.
export function useStepUp(): StepUp {
    const [pending, setPending] = useState<Pending>();
    const ask = useCallback(
        (mode: ReauthMode, resume: () => Promise<void>) =>
            setPending({ mode, resume, tag: sessionTag() }),
        [],
    );
    const cancel = useCallback(() => setPending(undefined), []);
    return useMemo(() => ({ pending, ask, cancel }), [pending, ask, cancel]);
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
            <ReauthenticationForm
                key={pending.mode}
                mode={pending.mode}
                email={email}
                autoFocus
                onCancel={stepUp.cancel}
                onVerified={async () => {
                    stepUp.cancel();
                    if (sessionTag() !== pending.tag) {
                        onError(errors("sessionChanged"));
                        return;
                    }
                    try {
                        await pending.resume();
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
