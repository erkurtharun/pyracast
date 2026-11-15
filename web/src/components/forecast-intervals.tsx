"use client";
import { useLocale, useTranslations } from "next-intl";
import { ScrollRegion } from "./scroll-region";

export type ForecastInterval = {
    start: string;
    end: string;
    ghi_wm2?: number | null;
    mean_power_mw?: number | null;
    energy_mwh?: number | null;
    quality_flags?: string[];
    source_snapshot_ids?: number[];
    source_kinds?: ("forecast" | "estimated_actuals")[];
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
    const locale = useLocale();
    const numberFormatter = new Intl.NumberFormat(locale, {
        maximumFractionDigits: 3,
    });

    return (
        <>
            <span className="interval-value">
                {value == null ? "—" : numberFormatter.format(value)}
            </span>
            {value == null && (
                <span className="sr-only">{t("data_unavailable")}</span>
            )}
            {electrical && (
                <span className="sr-only">
                    {power == null
                        ? t("_average_power_unavailable")
                        : t("average_power", {
                              power: numberFormatter.format(power),
                          })}
                </span>
            )}
        </>
    );
}

export function ForecastIntervals({
    rows,
    electrical,
    scenarioLabel,
    label,
    timezone,
    formatTime,
    showSources = false,
}: {
    rows: ForecastInterval[];
    electrical: boolean;
    scenarioLabel?: string;
    label: string;
    timezone: string;
    formatTime: (value: string) => string;
    showSources?: boolean;
}) {
    const t = useTranslations("ForecastIntervals");
    const locale = useLocale();
    const quality = useTranslations("Forecast.qualityFlags");
    const flagLabel = (flag: string) =>
        quality.has(flag)
            ? quality(flag)
            : quality("unknown", {
                  value: locale === "en" ? flag.replaceAll("_", " ") : flag,
              });

    const valueField = electrical ? "energy_mwh" : "ghi_wm2";
    const notes = rows.filter(
        (row) =>
            row.quality_flags?.length ||
            (scenarioLabel && row.affected_quality_flags?.length),
    );
    const missing = rows.some(
        (row) =>
            row[valueField] == null ||
            (scenarioLabel && row.affected_energy_mwh == null),
    );
    const intervalTime = (row: ForecastInterval) => (
        <>
            <time dateTime={row.start}>{formatTime(row.start)}</time>
            {"–"}
            <time dateTime={row.end}>{formatTime(row.end)}</time>
        </>
    );
    return (
        <details className="interval-details">
            <summary>{t("view_intervals", { count: rows.length })}</summary>
            <ScrollRegion label={label}>
                <table className="forecast-interval-table">
                    <caption className="sr-only">
                        {label}, {timezone}
                    </caption>
                    <thead>
                        <tr>
                            <th scope="col">{t("time_interval")}</th>
                            <th scope="col">
                                {electrical
                                    ? scenarioLabel
                                        ? t("normal_capacity")
                                        : t("energy")
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
                            {showSources && (
                                <th scope="col" className="interval-source">
                                    {t("source")}
                                </th>
                            )}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.start}>
                                <td>{intervalTime(row)}</td>
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
                                {showSources && (
                                    <td className="interval-source">
                                        {row.source_kinds?.length
                                            ? row.source_kinds.length > 1
                                                ? t("estimate_and_forecast")
                                                : row.source_kinds[0] ===
                                                    "estimated_actuals"
                                                  ? t("updated_estimate")
                                                  : t("forecast")
                                            : "—"}
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
            {notes.length > 0 && (
                <details className="interval-notes">
                    <summary>
                        {t("interval_notes", { count: notes.length })}
                    </summary>
                    <ul>
                        {notes.map((row) => (
                            <li key={row.start}>
                                <strong>{intervalTime(row)}</strong>
                                <div>
                                    {!!row.quality_flags?.length && (
                                        <p>
                                            {scenarioLabel
                                                ? t("normal_capacity_notes", {
                                                      flags: row.quality_flags
                                                          .map(flagLabel)
                                                          .join(", "),
                                                  })
                                                : row.quality_flags
                                                      .map(flagLabel)
                                                      .join(", ")}
                                        </p>
                                    )}
                                    {scenarioLabel &&
                                        !!row.affected_quality_flags
                                            ?.length && (
                                            <p>
                                                {t("scenario_notes", {
                                                    scenario: scenarioLabel,
                                                    flags: row.affected_quality_flags
                                                        .map(flagLabel)
                                                        .join(", "),
                                                })}
                                            </p>
                                        )}
                                </div>
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </details>
    );
}
