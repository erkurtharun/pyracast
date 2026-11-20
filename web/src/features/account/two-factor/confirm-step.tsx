"use client";
import { useTranslations } from "next-intl";
import type { Ref } from "react";

export function ConfirmStep({
    headingRef,
    action,
    busy,
    working,
    onConfirm,
    onCancel,
}: {
    headingRef: Ref<HTMLHeadingElement>;
    action: "replace" | "remove";
    busy: boolean;
    working: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}) {
    const t = useTranslations("Security");
    const replace = action === "replace";
    return (
        <div className="mfa-subpanel">
            <h3 ref={headingRef} tabIndex={-1}>
                {replace ? t("replaceConfirmTitle") : t("removeConfirmTitle")}
            </h3>
            <p>{replace ? t("replaceConfirmBody") : t("removeConfirmBody")}</p>
            <div className="button-group mfa-actions">
                <button
                    className={replace ? "button" : "button danger"}
                    type="button"
                    disabled={busy}
                    onClick={onConfirm}
                >
                    {working
                        ? t("working")
                        : replace
                          ? t("confirmReplace")
                          : t("confirmRemove")}
                </button>
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onCancel}
                >
                    {t("cancel")}
                </button>
            </div>
        </div>
    );
}
