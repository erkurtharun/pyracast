"use client";
import { useTranslations } from "next-intl";
import { Tag } from "@/ui/ui";
import type { TwoFactorView } from "./model";

export function TwoFactorStatusTag({
    view,
    mfaRequired,
}: {
    view: TwoFactorView;
    mfaRequired: boolean;
}) {
    const t = useTranslations("Security");
    if (view.kind === "loading" || view.kind === "loadError") return null;
    if (view.kind === "disabled" || view.kind === "enrolling") {
        return (
            <Tag tone={mfaRequired ? "warning" : "neutral"}>
                {mfaRequired ? t("statusRequired") : t("statusDisabled")}
            </Tag>
        );
    }
    return <Tag tone="success">{t("statusEnabled")}</Tag>;
}
