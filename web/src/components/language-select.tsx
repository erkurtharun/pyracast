"use client";

import { useId, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { request, type Session } from "@/lib/api";
import { isLanguage } from "@/i18n/language";

export function LanguageSelect({
    session,
    onSession,
}: {
    session: Session;
    onSession: (session: Session) => void;
}) {
    const t = useTranslations("Language");
    const locale = useLocale();
    const id = useId();
    const [state, setState] = useState<{ status: "idle" | "saving" | "error" }>(
        { status: "idle" },
    );

    return (
        <div className="language-control">
            <div className="language-select">
                <label htmlFor={id}>{t("label")}</label>
                <select
                    id={id}
                    value={locale}
                    disabled={state.status === "saving"}
                    onChange={async (event) => {
                        const language = event.target.value;
                        if (!isLanguage(language)) return;
                        setState({ status: "saving" });
                        try {
                            const updated = await request<Session>(
                                "/api/session/language/",
                                "POST",
                                { language },
                                session.csrf_token,
                            );
                            onSession(updated);
                            setState({ status: "idle" });
                        } catch {
                            setState({ status: "error" });
                        }
                    }}
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
        </div>
    );
}
