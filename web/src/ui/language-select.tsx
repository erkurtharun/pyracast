"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { api } from "@/lib/api/client";
import { reloadPage } from "@/lib/on-demand";
import { adoptSession, useSessionState } from "@/lib/session-client";
import { loadCatalogue, useCatalogues } from "@/i18n/catalogue";
import { isLanguage, type Language } from "@/i18n/language";

export function LanguageSelect() {
    const t = useTranslations("Language");
    const locale = useLocale();
    const id = useId();
    const chosen = useSessionState().session?.language;
    const catalogues = useCatalogues();
    const [state, setState] = useState<
        | { status: "idle" | "saving" | "unavailable" }
        | { status: "error"; target: Language }
    >({ status: "idle" });
    if (state.status === "error" && state.target === locale)
        setState({ status: "idle" });
    const mounted = useRef(false);
    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
        };
    }, []);
    // A failed catalogue stays failed for the life of the document, so only a reload can show it:
    // one chosen here, or one another tab chose for this session.
    const unavailable =
        state.status === "unavailable" ||
        (isLanguage(chosen) &&
            chosen !== locale &&
            catalogues[chosen]?.status === "failed");

    return (
        <div className="language-control">
            <div className="language-select">
                <label className="sr-only" htmlFor={id}>
                    {t("label")}
                </label>
                <svg
                    width="18"
                    height="18"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    aria-hidden="true"
                >
                    <circle cx="12" cy="12" r="9" />
                    <ellipse cx="12" cy="12" rx="4" ry="9" />
                    <path d="M3 12h18" />
                </svg>
                <select
                    id={id}
                    value={locale}
                    disabled={state.status === "saving"}
                    onChange={(event) =>
                        void (async () => {
                            const language = event.target.value;
                            if (!isLanguage(language)) return;
                            setState({ status: "saving" });
                            try {
                                await loadCatalogue(language);
                            } catch {
                                if (mounted.current)
                                    setState({ status: "unavailable" });
                                return;
                            }
                            // A control that has gone updates nothing; RequireUser unmounts it
                            // when the session's owner changes.
                            if (!mounted.current) return;
                            try {
                                adoptSession(
                                    await api.post("/api/session/language/", {
                                        body: { language },
                                    }),
                                );
                                if (mounted.current)
                                    setState({ status: "idle" });
                            } catch {
                                if (mounted.current)
                                    setState({
                                        status: "error",
                                        target: language,
                                    });
                            }
                        })()
                    }
                >
                    <option value="en" lang="en">
                        English
                    </option>
                    <option value="tr" lang="tr">
                        Türkçe
                    </option>
                </select>
            </div>
            {state.status === "saving" && (
                <span role="status">{t("saving")}</span>
            )}
            {state.status === "error" && <p role="alert">{t("failed")}</p>}
            {unavailable && (
                <>
                    <p role="alert">{t("unavailable")}</p>
                    <button
                        className="text-button"
                        type="button"
                        onClick={reloadPage}
                    >
                        {t("reload")}
                    </button>
                </>
            )}
        </div>
    );
}
