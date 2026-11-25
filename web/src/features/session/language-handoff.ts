"use client";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api/client";
import { addressWithout, appQuery } from "@/lib/routes";
import {
    adoptSession,
    isHandoffFailure,
    loadSession,
    type Session,
} from "@/lib/session-client";

export type Handoff =
    { status: "ready" } | { status: "pending" | "failed"; retry: () => void };

export function useLanguageHandoff(current: Session | undefined): Handoff {
    const { language } = appQuery(useSearchParams());
    const [status, setStatus] = useState<"pending" | "failed" | "ready">(
        language ? "pending" : "ready",
    );
    useEffect(() => {
        if (!current || !language || status !== "pending") return;
        let active = true;
        const settled = (session: Session) =>
            Boolean(session.user) || session.language === language;
        const finish = () => {
            window.history.replaceState(
                window.history.state,
                "",
                addressWithout(new URL(window.location.href), ["language"]),
            );
            setStatus("ready");
        };
        if (settled(current)) finish();
        else {
            const sentAt = Date.now();
            api.post("/api/session/language/", { body: { language } })
                .then(async (updated) => {
                    if (!active) return;
                    const adoption = await adoptSession(updated, sentAt);
                    // The notice of a failed handoff offers the read that ends this hand-off.
                    if (adoption === "failed") return;
                    const shown =
                        adoption === "handed" || settled(await loadSession());
                    if (!active) return;
                    if (shown) finish();
                    else setStatus("failed");
                })
                .catch((error: unknown) => {
                    if (active && !isHandoffFailure(error)) setStatus("failed");
                });
        }
        return () => {
            active = false;
        };
    }, [current, language, status]);
    if (status === "ready" || !language) return { status: "ready" };
    return { status, retry: () => setStatus("pending") };
}
