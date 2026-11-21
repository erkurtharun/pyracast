"use client";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { routes } from "@/lib/routes";

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
        <Link
            className="pending-approval"
            href={routes.inbox(tenantId, requestId)}
            aria-label={t("waitingForApprovalOf", { subject })}
        >
            {t("waitingForApproval")}
        </Link>
    );
}
