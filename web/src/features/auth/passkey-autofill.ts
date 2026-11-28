"use client";

import { useEffect, useEffectEvent, useRef, useState } from "react";
import type { AuthResult } from "@/lib/auth-api";
import { assertPasskey } from "@/lib/passkeys";

export function usePasskeyAutofill(
    enabled: boolean,
    onResult: (result: AuthResult) => Promise<void>,
) {
    const finish = useEffectEvent(onResult);
    const lifetime = useRef<AbortController>(null);
    const [revision, setRevision] = useState(0);
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        if (!enabled) return;
        const scope = new AbortController();
        lifetime.current = scope;
        let attempt: AbortController | undefined;
        let deadline = 0;
        let selected = false;

        async function suggest() {
            if (
                scope.signal.aborted ||
                document.hidden ||
                selected ||
                (attempt && !attempt.signal.aborted && Date.now() < deadline)
            )
                return;
            // Stop offering the challenge before the server's five-minute expiry.
            // The next interaction starts a fresh one without polling an idle form.
            attempt?.abort();
            const controller = new AbortController();
            attempt = controller;
            const lifetimeMs = 285_000;
            deadline = Date.now() + lifetimeMs;
            const timer = window.setTimeout(
                () => controller.abort(),
                lifetimeMs,
            );
            const cancel = () => controller.abort();
            scope.signal.addEventListener("abort", cancel, { once: true });
            const { signal } = controller;
            try {
                if (
                    !(await PublicKeyCredential.isConditionalMediationAvailable?.())
                )
                    return;
                signal.throwIfAborted();
                const result = await assertPasskey("login", signal, () => {
                    selected = true;
                    setFailed(false);
                });
                if (result.meta?.is_authenticated || !signal.aborted)
                    await finish(result);
            } catch {
                if (attempt === controller && selected && !scope.signal.aborted)
                    setFailed(true);
            } finally {
                window.clearTimeout(timer);
                scope.signal.removeEventListener("abort", cancel);
                if (attempt === controller) {
                    attempt = undefined;
                    selected = false;
                }
            }
        }
        function interact(event: Event) {
            if (
                event.target instanceof HTMLInputElement &&
                event.target.autocomplete === "username webauthn"
            )
                void suggest();
        }
        function refresh() {
            void suggest();
        }
        const options = { signal: scope.signal };
        document.addEventListener("focusin", interact, options);
        document.addEventListener("pointerdown", interact, options);
        document.addEventListener("visibilitychange", refresh, options);
        window.addEventListener("pagehide", () => attempt?.abort(), options);
        window.addEventListener("pageshow", refresh, options);
        void suggest();
        return () => scope.abort();
    }, [enabled, revision]);

    return {
        failed: enabled && failed,
        pause: () => {
            lifetime.current?.abort();
            setFailed(false);
        },
        resume: () => setRevision((current) => current + 1),
    };
}
