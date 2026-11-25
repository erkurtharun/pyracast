"use client";
import { useTranslations } from "next-intl";
import type { PlantScope } from "@/lib/api";
import { useApiQuery } from "@/lib/api/query";
import { Resource } from "@/ui/resource";
import { Notice } from "@/ui/ui";

export function ProductionSourceDetails({ scope }: { scope: PlantScope }) {
    const t = useTranslations("ProductionHistory");
    const connection = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production-source/",
        { path: scope },
    );
    return (
        <details className="production-source">
            <summary>{t("source_details")}</summary>
            <Resource resource={connection} loading={t("loading_source")}>
                {(source) => (
                    <>
                        <p>
                            <strong>{source.name || "EPİAŞ"}</strong> ·{" "}
                            {source.connected
                                ? t("connected")
                                : t("not_connected")}
                        </p>
                        {!source.connected && <p>{t("source_disconnected")}</p>}
                        {source.connected && source.last_error && (
                            <Notice error>{source.last_error}</Notice>
                        )}
                    </>
                )}
            </Resource>
            <p className="fine">{t("correction_policy")}</p>
        </details>
    );
}
