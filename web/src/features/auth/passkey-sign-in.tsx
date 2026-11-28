"use client";

import { useTranslations } from "next-intl";
import { ApiError } from "@/lib/api/errors";
import type { AuthResult } from "@/lib/auth-api";
import {
    assertPasskey,
    cancelledPasskey,
    usePasskeySignal,
} from "@/lib/passkeys";
import { AsyncButton } from "@/ui/form";

export function PasskeySignIn({
    secondFactor,
    onResult,
    onStart,
    onFailure,
}: {
    secondFactor: boolean;
    onResult: (result: AuthResult) => Promise<void>;
    onStart: () => void;
    onFailure: () => void;
}) {
    const t = useTranslations("Passkeys");
    const signalForPasskey = usePasskeySignal();
    return (
        <AsyncButton
            permitted
            variant="secondary"
            errorTitle={t("failed")}
            onClick={async () => {
                onStart();
                const signal = signalForPasskey();
                try {
                    await onResult(
                        await assertPasskey(
                            secondFactor ? "authenticate" : "login",
                            signal,
                        ),
                    );
                } catch (error) {
                    if (signal.aborted) return;
                    onFailure();
                    if (cancelledPasskey(error))
                        throw new ApiError(t("cancelled"));
                    throw error;
                }
            }}
        >
            {t(secondFactor ? "verify" : "signIn")}
        </AsyncButton>
    );
}
