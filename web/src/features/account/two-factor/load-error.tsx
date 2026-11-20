"use client";
import { useTranslations } from "next-intl";
import type { TwoFactorView } from "./model";

export function LoadError({
    view,
    busy,
    onRetry,
}: {
    view: Extract<TwoFactorView, { kind: "loadError" }>;
    busy: boolean;
    onRetry: () => void;
}) {
    const t = useTranslations("Security");
    return (
        <div>
            <div className="notice error" role="alert">
                {view.reason === "sessionExpired"
                    ? t("sessionExpired")
                    : view.reason === "identityRequired"
                      ? t("confirmIdentityFirst")
                      : view.message || t("loadFailed")}
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
