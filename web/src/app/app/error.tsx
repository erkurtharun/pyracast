"use client";
import { useTranslations } from "next-intl";

export default function AppError({
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    const t = useTranslations("AppError");
    return (
        <main className="auth-page">
            <section className="panel narrow" aria-labelledby="app-error-title">
                <h1 id="app-error-title">{t("title")}</h1>
                <p>{t("description")}</p>
                <button
                    className="button"
                    type="button"
                    onClick={() => retry()}
                >
                    {t("retry")}
                </button>
            </section>
        </main>
    );
}
