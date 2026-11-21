"use client";
import { useTranslations } from "next-intl";

export function LoadError({
    message,
    busy,
    onRetry,
}: {
    message?: string;
    busy: boolean;
    onRetry: () => void;
}) {
    const t = useTranslations("Security");
    return (
        <div>
            <div className="notice error" role="alert">
                {message || t("loadFailed")}
            </div>
            <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={onRetry}
            >
                {t("retry")}
            </button>
        </div>
    );
}
