"use client";
import { useTranslations } from "next-intl";
import { routes } from "@/lib/routes";
import { AppLink } from "@/ui/app-link";

export function PendingApproval({
    tenantId,
    requestId,
    subject,
}: {
    tenantId: number;
    requestId: number;
    subject: string;
}) {
    const t = useTranslations("ChangeRequests");
    return (
        <AppLink
            className="pending-approval"
            href={routes.inbox(tenantId, requestId)}
            label={t("waitingForApprovalOf", { subject })}
        >
            {t("waitingForApproval")}
        </AppLink>
    );
}
