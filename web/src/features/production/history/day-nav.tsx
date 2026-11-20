"use client";
import { useTranslations } from "next-intl";
import { ControlField } from "@/ui/form";
import { shiftDay } from "./model";

export function ProductionDayNav({
    date,
    stepFrom,
    yesterday,
    disabled,
    onSelect,
}: {
    date: string;
    stepFrom: string;
    yesterday: string;
    disabled: boolean;
    onSelect: (day: string) => void;
}) {
    const t = useTranslations("ProductionHistory");
    return (
        <div className="archive-date-nav">
            <button
                className="button secondary"
                type="button"
                aria-label={t("previous_day")}
                disabled={disabled}
                onClick={() => onSelect(shiftDay(stepFrom, -1))}
            >
                ←
            </button>
            <ControlField label={t("day")}>
                <input
                    type="date"
                    value={date}
                    max={yesterday}
                    disabled={disabled}
                    onChange={(event) => onSelect(event.target.value)}
                />
            </ControlField>
            <button
                className="button secondary"
                type="button"
                aria-label={t("next_day")}
                disabled={disabled || stepFrom >= yesterday}
                onClick={() => onSelect(shiftDay(stepFrom, 1))}
            >
                →
            </button>
        </div>
    );
}
