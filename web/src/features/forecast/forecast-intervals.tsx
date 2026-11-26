"use client";
import { useTranslations } from "next-intl";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { ScrollRegion } from "@/ui/scroll-region";

export type ForecastInterval = {
    start: string;
    end: string;
    ghi_wm2?: number | null;
    mean_power_mw?: number | null;
    energy_mwh?: number | null;
    quality_flags?: string[];
    source_snapshot_ids?: number[];
    source_kinds?: string[];
    affected_mean_power_mw?: number | null;
    affected_energy_mwh?: number | null;
    affected_quality_flags?: string[];
};

function IntervalValue({
    value,
    power,
    electrical,
}: {
    value: number | null | undefined;
    power?: number | null;
    electrical: boolean;
}) {
    const t = useTranslations("ForecastIntervals");
    const format = useFormat();

    return (
        <>
            <span className="interval-value">
                {value == null
                    ? MISSING
                    : electrical
                      ? format.computed(value)
                      : format.rounded(value, "W/m²")}
            </span>
            {value == null && (
                <span className="sr-only">{t("data_unavailable")}</span>
            )}
            {electrical && (
                <span className="sr-only">
                    {power == null
                        ? t("_average_power_unavailable")
                        : t("average_power", {
                              power: format.rounded(power, "MW"),
                          })}
                </span>
            )}
        </>
    );
}

export function ForecastIntervals({
    rows,
    electrical,
    satellite,
    scenarioLabel,
    label,
    formatTime,
}: {
    rows: ForecastInterval[];
    electrical: boolean;
    satellite: boolean;
    scenarioLabel?: string;
    label: string;
    formatTime: (value: string) => string;
}) {
    const t = useTranslations("ForecastIntervals");

    const valueField = electrical ? "energy_mwh" : "ghi_wm2";
    const missing = rows.some(
        (row) =>
            row[valueField] == null ||
            (scenarioLabel && row.affected_energy_mwh == null),
    );
    return (
        <details className="interval-details">
            <summary>{t("view_intervals")}</summary>
            <ScrollRegion label={label}>
                <table className="forecast-interval-table">
                    <caption className="sr-only">{label}</caption>
                    <thead>
                        <tr>
                            <th scope="col">{t("time_interval")}</th>
                            <th scope="col">
                                {electrical
                                    ? scenarioLabel
                                        ? t("normal_capacity")
                                        : satellite
                                          ? t("energy")
                                          : t("forecast")
                                    : t("irradiance")}{" "}
                                <span className="interval-unit">
                                    ({electrical ? "MWh" : "W/m²"})
                                </span>
                            </th>
                            {scenarioLabel && (
                                <th scope="col">
                                    {scenarioLabel}{" "}
                                    <span className="interval-unit">(MWh)</span>
                                </th>
                            )}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.start}>
                                <td>
                                    <time dateTime={row.start}>
                                        {formatTime(row.start)}
                                    </time>
                                    {"–"}
                                    <time dateTime={row.end}>
                                        {formatTime(row.end)}
                                    </time>
                                </td>
                                <td>
                                    <IntervalValue
                                        value={row[valueField]}
                                        power={row.mean_power_mw}
                                        electrical={electrical}
                                    />
                                </td>
                                {scenarioLabel && (
                                    <td>
                                        <IntervalValue
                                            value={row.affected_energy_mwh}
                                            power={row.affected_mean_power_mw}
                                            electrical
                                        />
                                    </td>
                                )}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </ScrollRegion>
            {missing && (
                <p className="fine">{t("_data_unavailable_not_zero")}</p>
            )}
        </details>
    );
}
