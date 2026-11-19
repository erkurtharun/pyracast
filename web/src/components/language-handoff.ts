"use client";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { isLanguage, type Language } from "@/i18n/language";
import { api } from "@/lib/api/client";
import { adoptSession, type Session } from "@/lib/session-client";

export type Handoff =
    | { status: "ready" }
    | { status: "pending" | "failed"; language: Language; retry: () => void };

export function useLanguageHandoff(current: Session | undefined): Handoff {
    const requested = useSearchParams().get("language");
    const language = isLanguage(requested) ? requested : undefined;
    const [status, setStatus] = useState<"pending" | "failed" | "ready">(
        language ? "pending" : "ready",
    );
    useEffect(() => {
        if (!current || !language || status !== "pending") return;
        let active = true;
        const finish = (updated: Session) => {
            if (!active) return;
            const url = new URL(window.location.href);
            url.searchParams.delete("language");
            window.history.replaceState(
                window.history.state,
                "",
                `${url.pathname}${url.search}${url.hash}`,
            );
            adoptSession(updated);
            setStatus("ready");
        };
        if (current.user || current.language === language) finish(current);
        else
            api.post("/api/session/language/", { body: { language } })
                .then(finish)
                .catch(() => {
                    if (active) setStatus("failed");
                });
        return () => {
            active = false;
        };
    }, [current, language, status]);
    if (status === "ready" || !language) return { status: "ready" };
    return { status, language, retry: () => setStatus("pending") };
}
