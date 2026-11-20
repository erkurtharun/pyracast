"use client";
import { useTranslations } from "next-intl";
import type { ForecastInterval as Interval } from "../forecast-intervals";
import { useFormat } from "@/lib/format/use-format";

export function ForecastChart({
    rows,
    metric,
    unit,
    electrical,
    scenario,
    scenarioLabel,
    time,
}: {
    rows: Interval[];
    metric: "mean_power_mw" | "ghi_wm2";
    unit: string;
    electrical: boolean;
    scenario: boolean;
    scenarioLabel: string;
    time: (value: string) => string;
}) {
    const t = useTranslations("Forecast");
    const { number } = useFormat();
    const [firstRow] = rows;
    const lastRow = rows.at(-1);
    const max = Math.max(
        1,
        ...rows.map((row) =>
            typeof row[metric] === "number" ? row[metric]! : 0,
        ),
    );
    return (
        <>
            <div className="chart-title">
                <strong>
                    {electrical
                        ? t("power_across_the_day")
                        : t("irradiance_across_the_day")}
                </strong>
                <span>{unit}</span>
            </div>
            {scenario && (
                <div className="chart-legend">
                    <span>
                        <i className="normal" aria-hidden="true" />
                        {t("normal_capacity")}
                    </span>
                    <span>
                        <i className="affected" aria-hidden="true" />
                        {scenarioLabel}
                    </span>
                </div>
            )}
            <div
                className="chart"
                role="img"
                aria-label={t(
                    scenario
                        ? "scenario_chart"
                        : electrical
                          ? "power_chart"
                          : "irradiance_chart",
                )}
            >
                {rows.map((row, index) => (
                    <div
                        className={`chart-column ${row[metric] == null ? "missing" : ""}`}
                        key={index}
                        style={{
                            flexGrow:
                                Date.parse(row.end) - Date.parse(row.start),
                        }}
                        title={
                            scenario
                                ? t("scenario_tooltip", {
                                      time: time(row.start),
                                      normal: number(row[metric]),
                                      affected: number(
                                          row.affected_mean_power_mw,
                                      ),
                                      unit,
                                  })
                                : t("interval_tooltip", {
                                      time: time(row.start),
                                      value: number(row[metric]),
                                      unit,
                                  })
                        }
                    >
                        <div
                            style={{
                                height: `${typeof row[metric] === "number" ? (row[metric]! / max) * 100 : 0}%`,
                            }}
                        />
                        {scenario && (
                            <div
                                style={{
                                    height: `${typeof row.affected_mean_power_mw === "number" ? (row.affected_mean_power_mw / max) * 100 : 0}%`,
                                    background: "var(--blue-575)",
                                }}
                            />
                        )}
                    </div>
                ))}
            </div>
            <div className="chart-axis">
                <span>{firstRow && time(firstRow.start)}</span>
                <span>{t("local_time")}</span>
                <span>{lastRow && time(lastRow.end)}</span>
            </div>
        </>
    );
}
