"use client";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import type { Region } from "@/lib/api";
import { useFormat } from "@/lib/format/use-format";
import { isLanguage, type Language } from "@/i18n/language";
import { CheckboxField, Select } from "@/ui/form";
import { Notice } from "@/ui/ui";
import type { Answer } from "./reminder-draft";
import { useRowFocus } from "./row-focus";

export type Row = { id: number; text: string };
export const rowsOf = (values: string[]): Row[] =>
    (values.length ? values : [""]).map((text, id) => ({ id, text }));

type Fields = { enabled: boolean; times: Row[]; language: Language };
export type Change = {
    enabled: (enabled: boolean) => void;
    times: (apply: (rows: Row[]) => Row[]) => void;
    language: (language: Language) => void;
};

function DeliveryTimes({
    rows,
    limit,
    change,
}: {
    rows: Row[];
    limit: number | null;
    change: Change["times"];
}) {
    const t = useTranslations("Reminders");
    const { prefix, focusRow } = useRowFocus(rows);
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("deliveryTimes")}</legend>
            {rows.map((row, index) => (
                <div className="recipient-row" key={row.id}>
                    <label className="field" htmlFor={`${prefix}-${row.id}`}>
                        {t("deliveryTime", { index: index + 1 })}
                        <input
                            id={`${prefix}-${row.id}`}
                            type="time"
                            name="times"
                            value={row.text}
                            onChange={(event) => {
                                const text = event.target.value;
                                change((now) =>
                                    now.map((item) =>
                                        item.id === row.id
                                            ? { ...item, text }
                                            : item,
                                    ),
                                );
                            }}
                        />
                    </label>
                    {rows.length > 1 && (
                        <button
                            type="button"
                            className="button secondary"
                            aria-label={t("removeDeliveryTime", {
                                index: index + 1,
                            })}
                            onClick={() => {
                                const neighbour =
                                    rows[index + 1] ?? rows[index - 1];
                                if (neighbour) focusRow(neighbour.id);
                                change((now) =>
                                    now.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            <span aria-hidden="true">&times;</span>
                        </button>
                    )}
                </div>
            ))}
            <button
                type="button"
                className="button secondary"
                disabled={limit != null && rows.length >= limit}
                onClick={() => {
                    const id = Math.max(-1, ...rows.map((row) => row.id)) + 1;
                    focusRow(id);
                    change((now) => [...now, { id, text: "" }]);
                }}
            >
                {t("addTime")}
            </button>
            {limit != null && (
                <span className="fine">
                    {" "}
                    {t("quotaCount", {
                        count: rows.filter((row) => row.text).length,
                        limit,
                    })}
                </span>
            )}
        </fieldset>
    );
}

export function ReminderFields({
    answer,
    region,
    fields,
    change,
    children,
}: {
    answer: Answer;
    region: Region;
    fields: Fields;
    change: Change;
    children: ReactNode;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const limit = answer.daily_reminder_limit;
    return (
        <>
            <CheckboxField
                label={t("enableForecastEmailsForThisPlant")}
                name="enabled"
                checked={fields.enabled}
                onChange={change.enabled}
            />
            {region.status !== "approved" && (
                <Notice>{t("approvalRequired")}</Notice>
            )}
            <DeliveryTimes
                rows={fields.times}
                limit={limit}
                change={change.times}
            />
            {children}
            <Select
                label={t("emailLanguage")}
                name="language"
                value={fields.language}
                onChange={(event) => {
                    if (isLanguage(event.target.value))
                        change.language(event.target.value);
                }}
                required
            >
                <option value="en">English</option>
                <option value="tr">Türkçe</option>
            </Select>
            <details>
                <summary>{t("reminderDetails")}</summary>
                <p className="fine">
                    {t("reminderPolicy", {
                        limit: limit == null ? "?" : format.number(limit),
                    })}
                </p>
            </details>
        </>
    );
}
