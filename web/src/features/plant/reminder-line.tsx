"use client";
import { useTranslations } from "next-intl";
import type { components } from "@/lib/schema";
import { useFormat } from "@/lib/format/use-format";
import { useLabel } from "@/ui/vocabulary";
type Outcome = Pick<
    components["schemas"]["ReminderDeliveryOutput"],
    "scheduled_for" | "status" | "last_error"
>;
type ReminderSummary = components["schemas"]["ReminderSummaryOutput"];

export function ReminderOutcome({
    outcome,
    timezone,
}: {
    outcome: Outcome;
    timezone: string;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const label = useLabel();
    return (
        <>
            {t("lastEmail", {
                status: label("NotificationDeliveryStatus", outcome.status),
                scheduled: format.time(outcome.scheduled_for, timezone),
            })}
            {outcome.last_error &&
                ` ${label("NotificationDeliveryError", outcome.last_error)}`}
        </>
    );
}
export function ReminderLine({
    reminder,
    timezone,
}: {
    reminder: ReminderSummary;
    timezone: string;
}) {
    const t = useTranslations("Reminders");
    const label = useLabel();
    return (
        <p className="fine">
            {t("dailyEmail", { state: label("ReminderState", reminder.state) })}
            {reminder.last_delivery && (
                <>
                    {" · "}
                    <ReminderOutcome
                        outcome={reminder.last_delivery}
                        timezone={timezone}
                    />
                </>
            )}
        </p>
    );
}
