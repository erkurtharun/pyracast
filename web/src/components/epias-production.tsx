"use client";
import { useLocale, useTranslations } from "next-intl";
import { ScrollRegion } from "./scroll-region";

import { useState } from "react";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { Notice, useResource } from "./ui";
import { CsvDownload } from "./csv-download";
import { dataIntervalRange } from "@/lib/intervals";

type Connection = components["schemas"]["EpiasConnectionOutput"];
type History = components["schemas"]["EpiasHistoryOutput"];

function shiftDay(day: string, offset: number) {
    const date = new Date(`${day}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

export function EpiasProduction({ base }: { base: string }) {
    const t = useTranslations("EpiasProduction");
    const locale = useLocale();

    const yesterday = localDay("Europe/Istanbul", -1);
    const [date, setDate] = useState(yesterday);
    const [source, setSource] = useState("auto");
    const [revision, setRevision] = useState(0);
    const connection = useResource<Connection>(
        `${base}production-source/`,
        revision,
    );
    const data = connection.data;

    return (
        <section className="panel">
            <h2>{t("automatic_production")}</h2>
            <details>
                <summary>{t("sources_and_calibration_eligibility")}</summary>
                <p className="fine">
                    {t(
                        "automatic_selection_prefers_settlement_injection_uevm_then",
                    )}
                </p>
            </details>
            {connection.loading ? (
                <p role="status">{t("loading_connection")}</p>
            ) : connection.error ? (
                <Notice error>{connection.error}</Notice>
            ) : (
                data && (
                    <>
                        <p>
                            <strong>
                                {data.connected
                                    ? data.name || t("epia_plant")
                                    : "EPİAŞ"}
                            </strong>{" "}
                            ·{" "}
                            {data.connected
                                ? t("connected")
                                : t("not_connected")}
                        </p>
                        {!data.connected && (
                            <p className="fine">
                                {t(
                                    "contact_your_administrator_to_connect_epia",
                                )}
                                {data.archived_days > 0 &&
                                    t("_saved_production_is_still_available")}
                            </p>
                        )}
                        <p className="fine">
                            {t("archived_days", { count: data.archived_days })}
                            {data.connected && (
                                <>
                                    {t("collection_status", {
                                        count: data.pending_jobs,
                                        date: data.last_success_at
                                            ? new Date(
                                                  data.last_success_at,
                                              ).toLocaleString(locale, {
                                                  timeZone: "Europe/Istanbul",
                                              })
                                            : t("not_yet_collected"),
                                    })}
                                </>
                            )}
                        </p>
                        {data.connected && data.last_error && (
                            <Notice error>
                                {t("collection_error", {
                                    error: data.last_error,
                                })}
                            </Notice>
                        )}
                    </>
                )
            )}
            <h3>{t("reported_production_archive")}</h3>
            {data?.first_settlement_date && data.last_settlement_date && (
                <div className="button-group">
                    <p>
                        {t("settlement_dates", {
                            start: data.first_settlement_date,
                            end: data.last_settlement_date,
                        })}
                    </p>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() => {
                            setDate(data.last_settlement_date!);
                            setSource("settlement_injection");
                        }}
                    >
                        {t("latest_settlement_day")}
                    </button>
                </div>
            )}
            <div className="forecast-controls">
                <div className="archive-date-nav">
                    <button
                        className="button secondary"
                        aria-label={t("previous_day")}
                        onClick={() => setDate(shiftDay(date, -1))}
                    >
                        <span aria-hidden="true">←</span>
                    </button>
                    <label className="field">
                        {t("archive_date")}
                        <input
                            type="date"
                            value={date}
                            max={yesterday}
                            onChange={(event) => {
                                if (
                                    event.target.value &&
                                    event.target.value <= yesterday
                                )
                                    setDate(event.target.value);
                            }}
                        />
                    </label>
                    <button
                        className="button secondary"
                        aria-label={t("next_day")}
                        disabled={date >= yesterday}
                        onClick={() => setDate(shiftDay(date, 1))}
                    >
                        <span aria-hidden="true">→</span>
                    </button>
                </div>
                <label className="field">
                    {t("production_source")}
                    <select
                        value={source}
                        onChange={(event) => setSource(event.target.value)}
                    >
                        <option value="auto">{t("automatic")}</option>
                        <option value="realtime_generation">
                            {t("reported_production_provisional")}
                        </option>
                        <option value="settlement_injection">
                            {t("settlement_injection_uevm")}
                        </option>
                    </select>
                </label>
                <button
                    className="button secondary"
                    onClick={() => setRevision((value) => value + 1)}
                >
                    {t("refresh")}
                </button>
            </div>
            <EpiasArchive
                key={`${date}/${source}/${revision}`}
                base={base}
                date={date}
                source={source}
            />
        </section>
    );
}

function EpiasArchive({
    base,
    date,
    source,
}: {
    base: string;
    date: string;
    source: string;
}) {
    const t = useTranslations("EpiasProduction");
    const locale = useLocale();
    const blockers: Record<string, string> = {
        configuration_unavailable: t(
            "no_plant_configuration_is_available_for_calibration",
        ),
        configuration_mismatch: t(
            "this_acquisition_belongs_to_another_plant_configuration",
        ),
        source_disconnected: t(
            "your_administrator_must_connect_the_production_source",
        ),
        measurement_basis_mismatch: t(
            "provider_production_requires_the_plant_measurement_point",
        ),
        production_unavailable: t(
            "no_reported_production_is_available_for_calibration",
        ),
    };
    const sourceLabels: Record<string, string> = {
        realtime_generation: t("reported_production_provisional"),
        settlement_injection: t("settlement_injection_uevm"),
        mixed: t("combined_production"),
    };

    const history = useResource<History>(
        `${base}reported-production/?${new URLSearchParams({ date, data_source: source })}`,
    );
    const selectedHistory = history.data;
    const rows = selectedHistory?.readings ?? [];
    const snapshots = selectedHistory?.source_snapshots?.length
        ? selectedHistory.source_snapshots
        : selectedHistory?.snapshot
          ? [selectedHistory.snapshot]
          : [];
    const positiveRows = dataIntervalRange(
        rows,
        (row) => row.energy_mwh !== null && row.energy_mwh > 0,
    );
    const visibleRows = positiveRows.length
        ? positiveRows
        : dataIntervalRange(rows, (row) => row.energy_mwh !== null);
    const total = selectedHistory?.reported_total_mwh ?? null;
    const time = (value: string) =>
        new Date(value).toLocaleString(locale, {
            timeZone: selectedHistory?.timezone,
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        });
    return (
        <>
            {history.loading ? (
                <p role="status">{t("loading_reported_production")}</p>
            ) : history.error ? (
                <Notice error>{history.error}</Notice>
            ) : selectedHistory?.snapshot ? (
                <>
                    <CsvDownload
                        filename={`epias-${selectedHistory.source_kind}-${selectedHistory.date}`}
                        records={rows}
                        metadata={{
                            ...selectedHistory,
                            readings: undefined,
                        }}
                    />
                    <details>
                        <summary>{t("acquisition_details")}</summary>
                        {snapshots.map((snapshot) => (
                            <div key={snapshot.id}>
                                <p className="fine">
                                    {t("source_acquired", {
                                        source:
                                            sourceLabels[
                                                snapshot.source_kind
                                            ] ?? snapshot.source_kind,
                                        collected: new Date(
                                            snapshot.collected_at,
                                        ).toLocaleString(locale, {
                                            timeZone: selectedHistory.timezone,
                                        }),
                                        published: new Date(
                                            snapshot.available_at,
                                        ).toLocaleString(locale, {
                                            timeZone: selectedHistory.timezone,
                                        }),
                                    })}
                                </p>
                                <p className="fine">
                                    {t("acquisition_policy", {
                                        id: snapshot.id,
                                        policy: snapshot.normalization_policy,
                                    })}
                                </p>
                                <p className="fine">
                                    {t("payload_digest", {
                                        digest: snapshot.content_sha256,
                                    })}
                                </p>
                            </div>
                        ))}
                    </details>
                    <Notice>
                        {(selectedHistory.source_kind ===
                            "realtime_generation" ||
                            rows.some(
                                (row) =>
                                    row.source_kind === "realtime_generation",
                            )) &&
                            t(
                                "reported_production_is_provisional_and_may_differ",
                            )}
                        {selectedHistory.calibration_eligible
                            ? t(
                                  "eligible_for_calibration_when_valid_interval_coverage",
                              )
                            : (blockers[
                                  selectedHistory.calibration_blocker ?? ""
                              ] ??
                              t(
                                  "this_production_source_is_excluded_from_calibration",
                              ))}
                    </Notice>
                    <p className="fine">
                        EPİAŞ -{" "}
                        {sourceLabels[selectedHistory.source_kind ?? ""] ??
                            t("reported_production")}
                        {" - "}
                        {selectedHistory.timezone}
                    </p>
                    {!positiveRows.length &&
                        visibleRows.length > 0 &&
                        visibleRows.every(
                            (row) =>
                                row.energy_mwh === null || row.energy_mwh === 0,
                        ) && (
                            <p>
                                {t(
                                    "all_available_intervals_have_zero_production",
                                )}
                            </p>
                        )}
                    {!visibleRows.length && (
                        <p>
                            {t(
                                "no_interval_production_values_are_available_missing",
                            )}
                        </p>
                    )}
                    <p>
                        {total === null
                            ? t("incomplete_provider_hours_total_unavailable")
                            : t("reported_total", {
                                  total: total.toLocaleString(locale, {
                                      maximumFractionDigits: 3,
                                  }),
                              })}
                    </p>
                    <ScrollRegion label={t("epia_reported_hourly_production")}>
                        <table className="production-table">
                            <caption>
                                {t("epia_reported_hourly_production")}
                            </caption>
                            <thead>
                                <tr>
                                    <th>{t("local_interval")}</th>
                                    <th>{t("production_mwh")}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {visibleRows.map((row, index) => (
                                    <tr
                                        key={`${row.provider_timestamp}-${index}`}
                                    >
                                        <td>
                                            {time(row.period_start)}–
                                            {time(row.period_end)}
                                            {selectedHistory.source_kind ===
                                                "mixed" &&
                                                row.source_kind && (
                                                    <div className="fine">
                                                        {row.source_kind ===
                                                        "settlement_injection"
                                                            ? "UEVM"
                                                            : t("provisional")}
                                                    </div>
                                                )}
                                        </td>
                                        <td>
                                            {row.energy_mwh === null
                                                ? t("missing")
                                                : row.energy_mwh.toLocaleString(
                                                      locale,
                                                      {
                                                          maximumFractionDigits: 3,
                                                      },
                                                  )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </ScrollRegion>
                </>
            ) : (
                <p>{t("no_epia_data_has_been_archived_for")}</p>
            )}
        </>
    );
}
