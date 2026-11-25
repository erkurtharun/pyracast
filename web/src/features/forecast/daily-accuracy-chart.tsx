"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";

type Day = Pick<components["schemas"]["PerformanceDay"], "date" | "error_mwh">;

// The table of matched days below the chart carries the same values for assistive technology.
export function DailyAccuracyChart({ days }: { days: readonly Day[] }) {
    const t = useTranslations("ForecastPerformance");
    const { number } = useFormat();
    const largest = Math.max(
        Number.EPSILON,
        ...days.map((day) => Math.abs(day.error_mwh ?? 0)),
    );
    const [first] = days;
    const last = days.at(-1);
    return (
        <>
            <div className="chart-title">
                <strong>{t("daily_error_title")}</strong>
                <span>MWh</span>
            </div>
            <div
                className="chart"
                role="img"
                aria-label={t("daily_error_title")}
            >
                {days.map((day) => (
                    <div
                        key={day.date}
                        className={`chart-column ${day.error_mwh == null ? "missing" : ""}`}
                        title={t("daily_error_tooltip", {
                            date: day.date,
                            error: number(day.error_mwh),
                        })}
                    >
                        <div
                            style={{
                                height: `${(Math.abs(day.error_mwh ?? 0) / largest) * 100}%`,
                            }}
                        />
                    </div>
                ))}
            </div>
            <div className="chart-axis">
                <span>{first?.date}</span>
                <span>{last?.date}</span>
            </div>
        </>
    );
}
