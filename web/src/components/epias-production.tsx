"use client";
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
            <h2>Automatic production</h2>
            <details>
                <summary>Sources and calibration eligibility</summary>
                <p className="fine">
                    Automatic selection prefers settlement injection (UEVM),
                    then provisional reported production. Reported production
                    can support provisional calibration; UEVM matching net
                    export is preferred. Disconnecting collection retains the
                    archive.
                </p>
            </details>
            {connection.loading ? (
                <p role="status">Loading connection…</p>
            ) : connection.error ? (
                <Notice error>{connection.error}</Notice>
            ) : (
                data && (
                    <>
                        <p>
                            <strong>
                                {data.connected
                                    ? data.name || "EPİAŞ plant"
                                    : "EPİAŞ"}
                            </strong>{" "}
                            · {data.connected ? "Connected" : "Not connected"}
                        </p>
                        {!data.connected && (
                            <p className="fine">
                                Contact your administrator to connect EPİAŞ.
                                {data.archived_days > 0 &&
                                    " Saved production is still available."}
                            </p>
                        )}
                        <p className="fine">
                            Archived days: {data.archived_days}
                            {data.connected && (
                                <>
                                    {" · Pending jobs: "}
                                    {data.pending_jobs}
                                    {" · Last successful collection: "}
                                    {data.last_success_at
                                        ? new Date(
                                              data.last_success_at,
                                          ).toLocaleString()
                                        : "Not yet collected"}
                                </>
                            )}
                        </p>
                        {data.connected && data.last_error && (
                            <Notice error>
                                Collection error: {data.last_error}
                            </Notice>
                        )}
                    </>
                )
            )}
            <h3>Reported production archive</h3>
            {data?.first_settlement_date && data.last_settlement_date && (
                <div className="button-group">
                    <p>
                        Archived settlement injection (UEVM):{" "}
                        {data.first_settlement_date} to{" "}
                        {data.last_settlement_date}
                    </p>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() => {
                            setDate(data.last_settlement_date!);
                            setSource("settlement_injection");
                        }}
                    >
                        Latest settlement day
                    </button>
                </div>
            )}
            <div className="forecast-controls">
                <div className="archive-date-nav">
                    <button
                        className="button secondary"
                        aria-label="Previous day"
                        onClick={() => setDate(shiftDay(date, -1))}
                    >
                        <span aria-hidden="true">←</span>
                    </button>
                    <label className="field">
                        Archive date
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
                        aria-label="Next day"
                        disabled={date >= yesterday}
                        onClick={() => setDate(shiftDay(date, 1))}
                    >
                        <span aria-hidden="true">→</span>
                    </button>
                </div>
                <label className="field">
                    Production source
                    <select
                        value={source}
                        onChange={(event) => setSource(event.target.value)}
                    >
                        <option value="auto">Automatic</option>
                        <option value="realtime_generation">
                            Reported production (provisional)
                        </option>
                        <option value="settlement_injection">
                            Settlement injection (UEVM)
                        </option>
                    </select>
                </label>
                <button
                    className="button secondary"
                    onClick={() => setRevision((value) => value + 1)}
                >
                    Refresh
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

const sourceLabels: Record<string, string> = {
    realtime_generation: "Reported production (provisional)",
    settlement_injection: "Settlement injection (UEVM)",
    mixed: "Combined production",
};
const blockers: Record<string, string> = {
    configuration_unavailable:
        "No plant configuration is available for calibration.",
    configuration_mismatch:
        "This acquisition belongs to another plant configuration.",
    source_disconnected:
        "Your administrator must connect the production source before settlement injection can be used for calibration.",
    measurement_basis_mismatch:
        "Provider production requires the plant measurement point to be net export.",
    production_unavailable:
        "No reported production is available for calibration.",
};

function EpiasArchive({
    base,
    date,
    source,
}: {
    base: string;
    date: string;
    source: string;
}) {
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
    const total =
        (selectedHistory?.complete ?? selectedHistory?.snapshot?.complete) &&
        rows.every((row) => row.energy_mwh !== null)
            ? rows.reduce((sum, row) => sum + (row.energy_mwh ?? 0), 0)
            : null;
    const time = (value: string) =>
        new Date(value).toLocaleString("en-GB", {
            timeZone: selectedHistory?.timezone,
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        });
    return (
        <>
            {history.loading ? (
                <p role="status">Loading reported production…</p>
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
                        <summary>Acquisition details</summary>
                        {snapshots.map((snapshot) => (
                            <div key={snapshot.id}>
                                <p className="fine">
                                    {sourceLabels[snapshot.source_kind] ??
                                        snapshot.source_kind}{" "}
                                    - Collected{" "}
                                    {new Date(
                                        snapshot.collected_at,
                                    ).toLocaleString()}
                                    {" - Published "}
                                    {new Date(
                                        snapshot.available_at,
                                    ).toLocaleString()}
                                </p>
                                <p className="fine">
                                    Acquisition #{snapshot.id} - Policy:{" "}
                                    {snapshot.normalization_policy}
                                </p>
                                <p className="fine">
                                    Payload digest: {snapshot.content_sha256}
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
                            "Reported production is provisional and may differ from net export. Settlement injection (UEVM) is preferred. "}
                        {selectedHistory.calibration_eligible
                            ? "Eligible for calibration when valid interval coverage is complete."
                            : (blockers[
                                  selectedHistory.calibration_blocker ?? ""
                              ] ??
                              "This production source is excluded from calibration because its measurement point does not match net export.")}
                    </Notice>
                    <p className="fine">
                        EPİAŞ -{" "}
                        {sourceLabels[selectedHistory.source_kind ?? ""] ??
                            "Reported production"}
                        {" - "}
                        {selectedHistory.timezone}
                    </p>
                    {!positiveRows.length &&
                        visibleRows.length > 0 &&
                        visibleRows.every(
                            (row) =>
                                row.energy_mwh === null || row.energy_mwh === 0,
                        ) && (
                            <p>All available intervals have zero production.</p>
                        )}
                    {!visibleRows.length && (
                        <p>
                            No interval production values are available. Missing
                            values are not zero.
                        </p>
                    )}
                    <p>
                        {total === null
                            ? "Incomplete provider hours — total unavailable"
                            : `Sum of provider-reported hours: ${total.toLocaleString(undefined, { maximumFractionDigits: 3 })} MWh`}
                    </p>
                    <ScrollRegion label="EPİAŞ reported hourly production">
                        <table className="production-table">
                            <caption>EPİAŞ reported hourly production</caption>
                            <thead>
                                <tr>
                                    <th>Local interval</th>
                                    <th>Production (MWh)</th>
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
                                                            : "Provisional"}
                                                    </div>
                                                )}
                                        </td>
                                        <td>
                                            {row.energy_mwh === null
                                                ? "Missing"
                                                : row.energy_mwh.toLocaleString(
                                                      undefined,
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
                <p>No EPİAŞ data has been archived for this day.</p>
            )}
        </>
    );
}
