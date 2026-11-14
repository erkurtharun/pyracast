"use client";
import { useLocale, useTranslations } from "next-intl";
import { ScrollRegion } from "./scroll-region";
import { useEffect, useState } from "react";
import { CsvDownload } from "./csv-download";
import { dataIntervalRange } from "@/lib/intervals";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { Notice } from "./ui";

type History = components["schemas"]["ProductionHistoryOutput"];
type Draft = { energy: string; unit: string };

function shiftDay(day: string, amount: number) {
    const value = new Date(`${day}T12:00:00Z`);
    value.setUTCDate(value.getUTCDate() + amount);
    return value.toISOString().slice(0, 10);
}
export function ProductionHistory({
    base,
    configuration,
    token,
    onSaved,
}: {
    base: string;
    configuration: number;
    token: string;
    onSaved: () => void;
}) {
    const t = useTranslations("ProductionHistory");
    const locale = useLocale();
    const enumLabel = (value: string) =>
        value === ""
            ? ""
            : t.has(`values.${value}`)
              ? t(`values.${value}`)
              : t("unknown_value", {
                    value: locale === "en" ? value.replaceAll("_", " ") : value,
                });

    const reasons: Record<string, string> = {
        configuration_unavailable: t(
            "no_plant_configuration_for_this_interval",
        ),
        configuration_not_current: t(
            "this_interval_belongs_to_an_earlier_configuration",
        ),
        observation_overlap: t("a_saved_reading_overlaps_this_interval_see"),
        estimated_actuals_unavailable: t(
            "no_archived_estimated_actuals_for_this_interval",
        ),
        already_recorded: t("already_saved"),
    };

    const [date, setDate] = useState("");
    const [resolution, setResolution] = useState("60");
    const [history, setHistory] = useState<History>();
    const [loadedQuery, setLoadedQuery] = useState("");
    const [failedQuery, setFailedQuery] = useState("");
    const [version, setVersion] = useState(0);
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [saving, setSaving] = useState(false);
    const [unit, setUnit] = useState("kWh");
    const [drafts, setDrafts] = useState<Record<string, Draft>>({});
    const query = `${base}production/?daylight_only=true&resolution_minutes=${resolution}${date ? `&start_date=${date}&end_date=${date}` : ""}`;
    useEffect(() => {
        let current = true;
        request<History>(query)
            .then((data) => {
                if (!current) return;
                setHistory(data);
                setLoadedQuery(query);
                setDate((selected) => selected || data.end_date);
                setError("");
                setFailedQuery("");
            })
            .catch((failure: Error) => {
                if (current) {
                    setError(failure.message);
                    setFailedQuery(query);
                }
            });
        return () => {
            current = false;
        };
    }, [query, version]);
    const ready = history && query === loadedQuery;
    const datedTime = (value: string) =>
        new Intl.DateTimeFormat(locale, {
            timeZone: history?.timezone,
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            timeZoneName: "short",
        }).format(new Date(value));
    const day = ready ? history.days[0] : undefined;
    const visibleIntervals = ready
        ? dataIntervalRange(
              history.intervals,
              (row) =>
                  row.reference_available ||
                  row.observation != null ||
                  Boolean(drafts[`${row.starts_at}/${row.ends_at}`]?.energy),
          )
        : [];
    const offsetFormatter = new Intl.DateTimeFormat(locale, {
        timeZone: history?.timezone,
        timeZoneName: "shortOffset",
    });
    const offsets = new Set(
        visibleIntervals.flatMap((row) =>
            [row.starts_at, row.ends_at].map(
                (value) =>
                    offsetFormatter
                        .formatToParts(new Date(value))
                        .find((part) => part.type === "timeZoneName")?.value,
            ),
        ),
    );
    const timeFormatter = new Intl.DateTimeFormat(locale, {
        timeZone: history?.timezone,
        hour: "2-digit",
        minute: "2-digit",
        ...(offsets.size > 1 ? { timeZoneName: "shortOffset" as const } : {}),
    });
    const time = (value: string) => timeFormatter.format(new Date(value));
    const label = (start: string, end: string) => `${time(start)}–${time(end)}`;
    const pending = ready
        ? history.intervals.filter(
              (row) =>
                  row.can_enter &&
                  drafts[`${row.starts_at}/${row.ends_at}`]?.energy.trim(),
          ).length
        : 0;
    async function save() {
        if (!ready || saving) return;
        const submitted = history.intervals
            .filter(
                (row) =>
                    row.can_enter &&
                    drafts[`${row.starts_at}/${row.ends_at}`]?.energy.trim(),
            )
            .map((row) => ({
                ...row,
                key: `${row.starts_at}/${row.ends_at}`,
                draft: drafts[`${row.starts_at}/${row.ends_at}`],
            }));
        if (!submitted.length) return;
        setSaving(true);
        setError("");
        setMessage("");
        try {
            await request(
                `${base}observations/`,
                "POST",
                {
                    configuration_id: configuration,
                    unit: "MWh",
                    observations: submitted.map((row) => ({
                        starts_at: row.starts_at,
                        ends_at: row.ends_at,
                        energy:
                            Number(row.draft.energy) /
                            (row.draft.unit === "kWh" ? 1000 : 1),
                        quality: "valid",
                        source: "meter",
                    })),
                },
                token,
            );
            setDrafts((current) => {
                const next = { ...current };
                for (const row of submitted)
                    if (next[row.key] === row.draft) delete next[row.key];
                return next;
            });
            setMessage(t("saved_readings", { count: submitted.length }));
            setVersion((current) => current + 1);
            onSaved();
        } catch (failure) {
            setError((failure as Error).message);
        } finally {
            setSaving(false);
        }
    }
    const maxDate = history ? localDay(history.timezone, -1) : undefined;
    return (
        <section className="panel journal">
            <div className="section-heading">
                <div>
                    <h2>{t("production_journal")}</h2>
                </div>
            </div>
            <p className="fine">
                {t("blank_entries_stay_missing_saved_readings_are")}
            </p>
            <div className="journal-controls">
                <div className="journal-date-nav">
                    <button
                        type="button"
                        className="button secondary"
                        aria-label={t("previous_day")}
                        disabled={!date}
                        onClick={() => setDate(shiftDay(date, -1))}
                    >
                        ←
                    </button>
                    <label className="field">
                        {t("day")}
                        <input
                            type="date"
                            disabled={!history}
                            value={date}
                            max={maxDate}
                            onChange={(event) => setDate(event.target.value)}
                        />
                    </label>
                    <button
                        type="button"
                        className="button secondary"
                        aria-label={t("next_day")}
                        disabled={!date || date >= (maxDate ?? "")}
                        onClick={() => setDate(shiftDay(date, 1))}
                    >
                        →
                    </button>
                </div>
                <label className="field">
                    {t("meter_interval")}
                    <select
                        value={resolution}
                        onChange={(event) => setResolution(event.target.value)}
                    >
                        <option value="30">{t("30_minutes")}</option>
                        <option value="60">{t("60_minutes")}</option>
                    </select>
                </label>
                <label className="field">
                    {t("entry_energy_unit")}
                    <select
                        value={unit}
                        onChange={(event) => setUnit(event.target.value)}
                    >
                        <option>kWh</option>
                        <option>MWh</option>
                    </select>
                </label>
            </div>
            {history && (
                <p className="fine">
                    {history.available_start_date && history.available_end_date
                        ? t("available_dates", {
                              start: history.available_start_date,
                              end: history.available_end_date,
                              timezone: history.timezone,
                          })
                        : t("no_available_dates", {
                              timezone: history.timezone,
                          })}
                </p>
            )}
            {error && <Notice error>{error}</Notice>}
            {message && <Notice>{message}</Notice>}
            {!ready ? (
                failedQuery === query ? null : (
                    <p role="status">{t("loading_production_history")}</p>
                )
            ) : (
                <>
                    <details>
                        <summary>{t("journal_entry_rules")}</summary>
                        <p className="fine">
                            {t(
                                "archived_estimated_actuals_unlock_entry_complete_daylight",
                            )}
                        </p>
                    </details>
                    <CsvDownload
                        label={t("download_production_journal_csv")}
                        filename={`production-journal-${history.start_date}-${history.end_date}`}
                        records={history.intervals.map((row) => {
                            const draft =
                                drafts[`${row.starts_at}/${row.ends_at}`];
                            return {
                                ...row,
                                draft_energy: draft?.energy
                                    ? Number(draft.energy)
                                    : null,
                                draft_unit: draft?.unit ?? null,
                            };
                        })}
                        metadata={{
                            ...history,
                            intervals: undefined,
                            observations: undefined,
                        }}
                    />
                    {day && (
                        <div className="journal-summary">
                            <div>
                                <strong>
                                    {t("recorded_count", {
                                        count: day.recorded_count,
                                    })}
                                </strong>
                                <span>
                                    {t("missing_count", {
                                        missing: day.missing_count,
                                        available: day.available_count,
                                    })}
                                </span>
                            </div>
                            <div>
                                <strong>{t("daylight")}</strong>
                                <span>
                                    {day.daylight_status === "polar_day"
                                        ? t("sun_above_horizon_all_day")
                                        : day.daylight_status === "polar_night"
                                          ? t("no_daylight_this_day")
                                          : day.daylight_windows?.length > 1
                                            ? day.daylight_windows
                                                  .map((window) =>
                                                      label(
                                                          window.starts_at,
                                                          window.ends_at,
                                                      ),
                                                  )
                                                  .join(", ")
                                            : day.sunrise && day.sunset
                                              ? `${time(day.sunrise)} – ${time(day.sunset)}`
                                              : day.daylight_windows?.length
                                                ? day.daylight_windows
                                                      .map((window) =>
                                                          label(
                                                              window.starts_at,
                                                              window.ends_at,
                                                          ),
                                                      )
                                                      .join(", ")
                                                : t(
                                                      "daylight_times_unavailable",
                                                  )}
                                </span>
                            </div>
                        </div>
                    )}
                    <form
                        onSubmit={(event) => {
                            event.preventDefault();
                            void save();
                        }}
                    >
                        {visibleIntervals.length === 0 ? (
                            <p>
                                {t(
                                    "no_comparison_data_or_saved_meter_intervals",
                                )}
                            </p>
                        ) : (
                            <ScrollRegion label={t("meter_entries")}>
                                <table className="journal-table">
                                    <caption>
                                        {t("meter_caption", {
                                            minutes: resolution,
                                        })}
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>{t("local_interval")}</th>
                                            <th>{t("production")}</th>
                                            <th>{t("status")}</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {visibleIntervals.map((row) => {
                                            const key = `${row.starts_at}/${row.ends_at}`,
                                                draft = drafts[key];
                                            return (
                                                <tr key={key}>
                                                    <th scope="row">
                                                        {label(
                                                            row.starts_at,
                                                            row.ends_at,
                                                        )}
                                                    </th>
                                                    <td>
                                                        {row.observation ? (
                                                            `${new Intl.NumberFormat(locale, { maximumFractionDigits: 3 }).format(row.observation.energy_mwh)} MWh`
                                                        ) : (
                                                            <>
                                                                <input
                                                                    className="meter-input"
                                                                    aria-label={t(
                                                                        "production_at",
                                                                        {
                                                                            start: row.starts_at,
                                                                        },
                                                                    )}
                                                                    type="number"
                                                                    min="0"
                                                                    step="any"
                                                                    disabled={
                                                                        !row.can_enter
                                                                    }
                                                                    value={
                                                                        draft?.energy ??
                                                                        ""
                                                                    }
                                                                    onChange={(
                                                                        event,
                                                                    ) =>
                                                                        setDrafts(
                                                                            (
                                                                                current,
                                                                            ) => ({
                                                                                ...current,
                                                                                [key]: {
                                                                                    energy: event
                                                                                        .target
                                                                                        .value,
                                                                                    unit,
                                                                                },
                                                                            }),
                                                                        )
                                                                    }
                                                                />{" "}
                                                                {draft?.unit ??
                                                                    unit}
                                                            </>
                                                        )}
                                                    </td>
                                                    <td>
                                                        {row.observation
                                                            ? `${row.observation.quality} · ${row.observation.source}`
                                                            : row.can_enter
                                                              ? t("not_entered")
                                                              : (reasons[
                                                                    row.unavailable_reason ??
                                                                        ""
                                                                ] ??
                                                                t(
                                                                    "unavailable_for_entry",
                                                                ))}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </ScrollRegion>
                        )}
                        <div className="journal-save">
                            <span>
                                {t("pending_readings", { count: pending })}
                            </span>
                            <button
                                className="button"
                                type="submit"
                                disabled={saving || !pending}
                            >
                                {saving
                                    ? t("saving")
                                    : t("save_meter_readings")}
                            </button>
                        </div>
                    </form>
                    {history.observations.length > 0 && (
                        <details>
                            <summary>
                                {t("saved_reading_count", {
                                    count: history.observations.length,
                                })}
                            </summary>
                            <CsvDownload
                                label={t("download_saved_readings_csv")}
                                filename={`meter-readings-${history.start_date}-${history.end_date}`}
                                records={history.observations}
                                metadata={{
                                    timezone: history.timezone,
                                    plant_id: history.plant_id,
                                }}
                            />
                            <ScrollRegion
                                label={t("saved_production_readings")}
                            >
                                <table>
                                    <caption>
                                        {t(
                                            "saved_readings_original_intervals_and_configuration",
                                        )}
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>{t("local_interval")}</th>
                                            <th>{t("energy_mwh")}</th>
                                            <th>{t("quality")}</th>
                                            <th>{t("source")}</th>
                                            <th>{t("configuration")}</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {history.observations.map((row) => (
                                            <tr key={row.id}>
                                                <th>
                                                    {t("reading_interval", {
                                                        start: datedTime(
                                                            row.starts_at,
                                                        ),
                                                        end: datedTime(
                                                            row.ends_at,
                                                        ),
                                                    })}
                                                </th>
                                                <td>
                                                    {new Intl.NumberFormat(
                                                        locale,
                                                        {
                                                            maximumFractionDigits: 3,
                                                        },
                                                    ).format(row.energy_mwh)}
                                                </td>
                                                <td>
                                                    {enumLabel(row.quality)}
                                                </td>
                                                <td>{enumLabel(row.source)}</td>
                                                <td>{row.configuration_id}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </ScrollRegion>
                        </details>
                    )}
                </>
            )}
        </section>
    );
}
