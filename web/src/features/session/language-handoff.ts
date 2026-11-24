"use client";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api/client";
import { addressWithout, appQuery } from "@/lib/routes";
import { adoptSession, type Session } from "@/lib/session-client";

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
        const finish = () => {
            window.history.replaceState(
                window.history.state,
                "",
                addressWithout(new URL(window.location.href), ["language"]),
            );
            setStatus("ready");
        };
        if (current.user || current.language === language) finish();
        else {
            const sentAt = Date.now();
            api.post("/api/session/language/", { body: { language } })
                .then(async (updated) => {
                    if (active && (await adoptSession(updated, sentAt)))
                        finish();
                })
                .catch(() => {
                    if (active) setStatus("failed");
                });
        }
        return () => {
            active = false;
        };
    }, [current, language, status]);
    if (status === "ready" || !language) return { status: "ready" };
    return { status, retry: () => setStatus("pending") };
}
