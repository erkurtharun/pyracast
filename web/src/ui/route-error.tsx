"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { reloadPage } from "@/lib/on-demand";
import { loadSession } from "@/lib/session-client";
import { useErrorMessage } from "./form";

export function RouteError({ retry }: { retry: () => void }) {
    const t = useTranslations("AppError");
    const errorMessage = useErrorMessage();
    const [state, setState] = useState<
        { status: "idle" | "pending" } | { status: "failed"; message: string }
    >({ status: "idle" });
    return (
        <section className="panel narrow" aria-labelledby="app-error-title">
            <h1 id="app-error-title">{t("title")}</h1>
            <p>{t("description")}</p>
            {state.status === "failed" && <p role="alert">{state.message}</p>}
            <button
                className="button"
                type="button"
                disabled={state.status === "pending"}
                onClick={() =>
                    void (async () => {
                        setState({ status: "pending" });
                        try {
                            await loadSession();
                            retry();
                            setState({ status: "idle" });
                        } catch (error) {
                            setState({
                                status: "failed",
                                message: errorMessage(error),
                            });
                        }
                    })()
                }
            >
                {t("retry")}
            </button>
            <button className="text-button" type="button" onClick={reloadPage}>
                {t("reload")}
            </button>
        </section>
    );
}
