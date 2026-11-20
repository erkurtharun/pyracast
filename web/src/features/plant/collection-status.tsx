"use client";
import { useTranslations } from "next-intl";
import type { components } from "@/lib/schema";
import { useFormat } from "@/lib/format/use-format";
import { Notice, Stat } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";

type Schedule = components["schemas"]["CollectionScheduleOutput"];

export function CollectionStatus({ data }: { data: Schedule }) {
    const t = useTranslations("CollectionSchedule");
    const format = useFormat();
    const label = useLabel();
    const moment = (value: string | null, missing: string) =>
        value ? format.time(value, data.timezone) : missing;
    return (
        <section className="panel" aria-labelledby="collection-status-title">
            <h3 id="collection-status-title">{t("collectionStatus")}</h3>
            <p>
                <strong>{label("CollectionState", data.state)}</strong>
            </p>
            {data.inactive_reason && (
                <Notice>
                    {label("CollectionInactiveReason", data.inactive_reason)}
                </Notice>
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
                <Stat
                    label={t("requestsToday")}
                    value={format.number(data.requests_today)}
                />
            </div>
        </section>
    );
}
