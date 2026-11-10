"use client";

import { useState } from "react";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { ActionForm, Notice, useResource } from "./ui";
import { CsvDownload } from "./csv-download";
import { dataIntervalRange } from "@/lib/intervals";

type Connection = components["schemas"]["EpiasConnectionOutput"];
type History = components["schemas"]["EpiasHistoryOutput"];

function shiftDay(day: string, offset: number) {
    const date = new Date(`${day}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

export function EpiasProduction({
    base,
    token,
}: {
    base: string;
    token: string;
}) {
    const yesterday = localDay("Europe/Istanbul", -1);
    const [date, setDate] = useState(yesterday);
    const [source, setSource] = useState("auto");
    const [revision, setRevision] = useState(0);
    const [editing, setEditing] = useState(false);
    const [error, setError] = useState("");
    const [working, setWorking] = useState(false);
    const connection = useResource<Connection>(
        `${base}production-source/`,
        revision,
    );
    const data = connection.data;

    async function disconnect() {
        setWorking(true);
        setError("");
        try {
            await request(
                `${base}production-source/`,
                "DELETE",
                undefined,
                token,
            );
            setEditing(false);
            setRevision((value) => value + 1);
        } catch (reason) {
            setError((reason as Error).message);
        } finally {
            setWorking(false);
        }
    }

    return (
        <section className="panel">
            <h2>Automatic production</h2>
            <p>
                Automatically reported hourly production from EPİAŞ. Archived
                data remains available when collection is disconnected.
            </p>
            <p className="fine">
                Automatic selection prefers settlement injection (UEVM), then
                provisional reported production. Only settlement injection
                matching the plant's net-export measurement point can enter
                calibration.
            </p>
            {error && <Notice error>{error}</Notice>}
            {connection.loading ? (
                <p role="status">Loading connection…</p>
            ) : connection.error ? (
                <Notice error>{connection.error}</Notice>
            ) : (
                data && (
                    <>
                        {data.connected && !editing && (
                            <>
                                <p>
                                    <strong>
                                        {data.name || "EPİAŞ plant"}
                                    </strong>{" "}
                                    · ID {data.provider_plant_id} · {data.eic}
                                </p>
                                <p className="fine">
                                    Measurement point:{" "}
                                    {data.meter_basis.replaceAll("_", " ")} ·
                                    Archived days: {data.archived_days} ·
                                    Pending jobs: {data.pending_jobs}
                                </p>
                                <p className="fine">
                                    Last successful collection:{" "}
                                    {data.last_success_at
                                        ? new Date(
                                              data.last_success_at,
                                          ).toLocaleString()
                                        : "Not yet collected"}
                                </p>
                                {data.last_error && (
                                    <Notice error>
                                        Collection error: {data.last_error}
                                    </Notice>
                                )}
                                <div className="button-group">
                                    <button
                                        className="button secondary"
                                        onClick={() => setEditing(true)}
                                    >
                                        Edit connection
                                    </button>
                                    <button
                                        className="button secondary"
                                        disabled={working}
                                        onClick={disconnect}
                                    >
                                        Disconnect collection
                                    </button>
                                </div>
                            </>
                        )}
                        {(!data.connected || editing) && (
                            <ActionForm
                                submit={
                                    data.connected
                                        ? "Save connection"
                                        : "Connect EPİAŞ"
                                }
                                onSubmit={async (form) => {
                                    await request<Connection>(
                                        `${base}production-source/`,
                                        "PUT",
                                        {
                                            provider_plant_id: Number(
                                                form.get("provider_plant_id"),
                                            ),
                                            expected_eic: String(
                                                form.get("expected_eic") ?? "",
                                            ).trim(),
                                            meter_basis:
                                                form.get("meter_basis"),
                                        },
                                        token,
                                    );
                                    setEditing(false);
                                    setRevision((value) => value + 1);
                                }}
                            >
                                <div className="form-grid">
                                    <label className="field">
                                        EPİAŞ plant ID
                                        <input
                                            name="provider_plant_id"
                                            type="number"
                                            min="1"
                                            step="1"
                                            required
                                            defaultValue={
                                                data.provider_plant_id ?? ""
                                            }
                                        />
                                    </label>
                                    <label className="field">
                                        Plant EIC
                                        <input
                                            name="expected_eic"
                                            required
                                            maxLength={16}
                                            defaultValue={data.eic}
                                        />
                                    </label>
                                    <label className="field">
                                        Measurement point
                                        <select
                                            name="meter_basis"
                                            defaultValue={
                                                data.meter_basis || "unknown"
                                            }
                                        >
                                            <option value="unknown">
                                                Unknown
                                            </option>
                                            <option value="net_export">
                                                Net export
                                            </option>
                                            <option value="ac_generation">
                                                AC generation
                                            </option>
                                        </select>
                                    </label>
                                </div>
                                {data.connected && (
                                    <button
                                        type="button"
                                        className="button secondary"
                                        onClick={() => setEditing(false)}
                                    >
                                        Cancel
                                    </button>
                                )}
                            </ActionForm>
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
            <div className="button-group">
                <button
                    className="button secondary"
                    onClick={() => setDate(shiftDay(date, -1))}
                >
                    Previous day
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
                    disabled={date >= yesterday}
                    onClick={() => setDate(shiftDay(date, 1))}
                >
                    Next day
                </button>
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
};
const blockers: Record<string, string> = {
    provisional_production:
        "Provisional reported production is excluded from calibration. Use settlement injection (UEVM).",
    configuration_unavailable:
        "No plant configuration is available for calibration.",
    configuration_mismatch:
        "This acquisition belongs to another plant configuration.",
    source_disconnected:
        "Connect the production source before using settlement injection for calibration.",
    measurement_basis_mismatch:
        "Settlement injection requires the plant and connection measurement points to be net export.",
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
    const positiveRows = dataIntervalRange(
        rows,
        (row) => row.energy_mwh !== null && row.energy_mwh > 0,
    );
    const visibleRows = positiveRows.length
        ? positiveRows
        : dataIntervalRange(rows, (row) => row.energy_mwh !== null);
    const total =
        selectedHistory?.snapshot?.complete &&
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
                        metadata={{ ...selectedHistory, readings: undefined }}
                    />
                    <Notice>
                        {selectedHistory.calibration_eligible
                            ? "Settlement injection is eligible for calibration when valid interval coverage is complete."
                            : (blockers[
                                  selectedHistory.calibration_blocker ?? ""
                              ] ??
                              "This production source is excluded from calibration because its measurement point does not match net export.")}
                    </Notice>
                    <p className="fine">
                        Source: EPİAŞ ·{" "}
                        {sourceLabels[selectedHistory.source_kind ?? ""] ??
                            "Reported production"}{" "}
                        · Collected{" "}
                        {new Date(
                            selectedHistory.snapshot.collected_at,
                        ).toLocaleString()}{" "}
                        · Times shown in {selectedHistory.timezone}
                    </p>
                    {positiveRows.length > 0 &&
                        visibleRows.length < rows.length && (
                            <p className="fine">
                                Showing the first through last positive
                                interval, including gaps. CSV includes all
                                intervals.
                            </p>
                        )}
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
                    <div className="table-scroll">
                        <table>
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
                    </div>
                </>
            ) : (
                <p>No EPİAŞ data has been archived for this day.</p>
            )}
        </>
    );
}
