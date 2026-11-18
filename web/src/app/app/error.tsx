"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { loadSession } from "@/lib/session-client";

export default function AppError({
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    const t = useTranslations("AppError");
    const [state, setState] = useState<
        { status: "idle" | "pending" } | { status: "failed"; message: string }
    >({ status: "idle" });
    return (
        <main className="auth-page">
            <section className="panel narrow" aria-labelledby="app-error-title">
                <h1 id="app-error-title">{t("title")}</h1>
                <p>{t("description")}</p>
                {state.status === "failed" && (
                    <p role="alert">{state.message}</p>
                )}
                <button
                    className="button"
                    type="button"
                    disabled={state.status === "pending"}
                    onClick={async () => {
                        setState({ status: "pending" });
                        try {
                            await loadSession();
                            retry();
                            setState({ status: "idle" });
                        } catch (error) {
                            setState({
                                status: "failed",
                                message: (error as Error).message,
                            });
                        }
                    }}
                >
                    {t("retry")}
                </button>
            </section>
        </main>
    );
}
