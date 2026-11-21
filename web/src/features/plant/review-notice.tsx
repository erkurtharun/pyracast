"use client";
import { useTranslations } from "next-intl";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useRefresh } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import { contactEmail } from "@/lib/public-metadata";
import { AsyncButton } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";

const DECISION_TIME: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
};

export function ReviewNotice({
    region,
    scope,
    timezone,
}: {
    region: Region;
    scope: RegionScope;
    timezone: string;
}) {
    const t = useTranslations("PlantWorkspace");
    const format = useFormat();
    const label = useLabel();
    const refresh = useRefresh();
    if (region.status === "approved") return null;
    if (region.status === "pending")
        return <Notice>{t("awaitingApproval")}</Notice>;
    return (
        <Notice>
            <p>
                {region.status === "rejected"
                    ? t("rejectedNotice")
                    : t("approvalStatus", {
                          status: label("RegionStatus", region.status),
                          contact: contactEmail,
                      })}
            </p>
            {region.review_reason && region.reviewed_at && (
                <p>
                    {t("reviewReason", {
                        date: format.time(
                            region.reviewed_at,
                            timezone,
                            DECISION_TIME,
                        ),
                        reason: region.review_reason,
                    })}
                </p>
            )}
            {region.status === "rejected" && (
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
