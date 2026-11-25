"use client";
import { useTranslations } from "next-intl";
import type { RegionScope, Site } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useRefresh } from "@/lib/api/query";
import { contactEmail } from "@/lib/public-metadata";
import { AsyncButton } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";

export function ReviewNotice({
    region,
    scope,
}: {
    region: Site;
    scope: RegionScope;
}) {
    const t = useTranslations("PlantWorkspace");
    const dashboard = useTranslations("Dashboard");
    const label = useLabel();
    const refresh = useRefresh();
    const canManage = useCanManageWorkspace();
    if (region.status === "approved")
        return region.attention === "connection_unavailable" ? (
            <Notice>
                {dashboard("attentionReason", {
                    reason: label("SiteAttention", region.attention),
                })}
            </Notice>
        ) : null;
    if (region.status === "pending")
        return (
            <Notice>
                <p>{t("awaitingApproval")}</p>
                {canManage && <p>{t("stillEditable")}</p>}
            </Notice>
        );
    const rejected = region.status === "rejected";
    return (
        <Notice>
            <p>
                {rejected
                    ? t("rejectedNotice")
                    : t("approvalStatus", { contact: contactEmail })}
            </p>
            {region.review_reason && <p>{region.review_reason}</p>}
            {rejected && canManage && (
                <>
                    <p>{t("resubmitHelp")}</p>
                    <AsyncButton
                        success={t("resubmitted")}
                        onClick={async () => {
                            await api.post(
                                "/api/tenants/{tenant_id}/regions/{region_id}/resubmit/",
                                {
                                    path: {
                                        tenant_id: scope.tenant_id,
                                        region_id: scope.region_id,
                                    },
                                    body: {
                                        expected_revision: region.revision,
                                    },
                                },
                            );
                            await refresh.workspace(scope.tenant_id);
                        }}
                    >
                        {t("resubmit")}
                    </AsyncButton>
                </>
            )}
        </Notice>
    );
}
