"use client";
import { useTranslations } from "next-intl";
import type { components } from "@/lib/schema";
import type { Region } from "@/lib/api";
import { useFormat } from "@/lib/format/use-format";
import { Notice, Stat } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";

type Schedule = components["schemas"]["CollectionScheduleOutput"];

export function CollectionStatus({
    data,
    region,
}: {
    data: Schedule;
    region: Region;
}) {
    const t = useTranslations("CollectionSchedule");
    const format = useFormat();
    const label = useLabel();
    const moment = (value: string | null, missing: string) =>
        value ? format.time(value, data.timezone) : missing;
    // The plant workspace banner already says why a plant that is not approved does not collect.
    const reason = region.status === "approved" ? data.inactive_reason : null;
    return (
        <section className="panel" aria-labelledby="collection-status-title">
            <h3 id="collection-status-title">{t("collectionStatus")}</h3>
            <p>
                <strong>{label("CollectionState", data.state)}</strong>
            </p>
            {reason && (
                <Notice>{label("CollectionInactiveReason", reason)}</Notice>
            )}
            <div className="stats">
                <Stat
                    label={t("lastSuccess")}
                    value={moment(data.last_success_at, t("notYet"))}
                />
                <Stat
                    label={t("nextCollection")}
                    value={moment(data.next_collection_at, t("notScheduled"))}
                />
            </div>
        </section>
    );
}
