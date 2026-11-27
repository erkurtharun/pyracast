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
    const flows = (result.data as {
        flows?: { id?: unknown; types?: string[] }[];
    } | undefined)?.flows;
    const flow = flows?.[0];
    const id = flow?.id;
    if (id === "mfa_reauthenticate" && flow?.types?.includes("webauthn")) {
        return flow.types.some((type) => type === "totp" || type === "recovery_codes")
            ? "mfaOrPasskey"
            : "passkey";
    }
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
    const passkeys = useTranslations("Passkeys");
    const errors = useTranslations("Errors");
    const { pending } = stepUp;
    if (!pending) return null;
    const title = {
        mfa: t("reauthMfaTitle"),
        email: t("emailReauthTitle"),
        password: t("reauthPasswordTitle"),
        passkey: passkeys("verify"),
        mfaOrPasskey: passkeys("verifyOptions"),
    }[pending.mode];
    const description = {
        mfa: t("reauthMfaDescription"),
        email: undefined,
        password: t("reauthPasswordDescription"),
        passkey: undefined,
        mfaOrPasskey: passkeys("verifyDescription"),
    }[pending.mode];
    return (
        <div className="mfa-subpanel step-up">
            <h3>{title}</h3>
            {description && <p>{description}</p>}
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
