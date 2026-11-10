"use client";

import { useState } from "react";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { ActionForm, Notice, useResource } from "./ui";

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
    const [revision, setRevision] = useState(0);
    const [editing, setEditing] = useState(false);
    const [error, setError] = useState("");
    const [working, setWorking] = useState(false);
    const connection = useResource<Connection>(
        `${base}production-source/`,
        revision,
    );
    const history = useResource<History>(
        `${base}reported-production/?date=${date}`,
        revision,
    );
    const data = connection.data;
    const selectedHistory =
        history.data?.date === date ? history.data : undefined;
    const rows = selectedHistory?.readings ?? [];
    const hours = new Set(
        rows.map((row) => {
            const stamp = new Date(row.provider_timestamp);
            if (Number.isNaN(stamp.getTime())) return "invalid";
            if (
                stamp.toLocaleDateString("en-CA", {
                    timeZone: "Europe/Istanbul",
                }) !== date ||
                stamp.getUTCSeconds() !== 0 ||
                stamp.getUTCMilliseconds() !== 0
            )
                return "invalid";
            return new Intl.DateTimeFormat("en-GB", {
                timeZone: "Europe/Istanbul",
                hour: "2-digit",
                minute: "2-digit",
                hourCycle: "h23",
            }).format(stamp);
        }),
    );
    const complete =
        rows.length === 24 &&
        hours.size === 24 &&
        [...hours].every((hour) => /^\d{2}:00$/.test(hour)) &&
        rows.every((row) => row.energy_mwh !== null);
    const total = complete
        ? rows.reduce((sum, row) => sum + (row.energy_mwh ?? 0), 0)
        : null;

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
            <Notice>
                Calibration is waiting for confirmation of the time interval
                convention and measurement point.
            </Notice>
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
            {history.loading || (!selectedHistory && !history.error) ? (
                <p role="status">Loading reported production…</p>
            ) : history.error ? (
                <Notice error>{history.error}</Notice>
            ) : selectedHistory?.snapshot ? (
                <>
                    <p className="fine">
                        Source: EPİAŞ · Collected{" "}
                        {new Date(
                            selectedHistory.snapshot.collected_at,
                        ).toLocaleString()}{" "}
                        · Times shown in {selectedHistory.timezone}
                    </p>
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
                                    <th>Provider time</th>
                                    <th>Production (MWh)</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map((row, index) => (
                                    <tr
                                        key={`${row.provider_timestamp}-${index}`}
                                    >
                                        <td>
                                            {new Date(
                                                row.provider_timestamp,
                                            ).toLocaleString(undefined, {
                                                timeZone:
                                                    history.data?.timezone,
                                                hour: "2-digit",
                                                minute: "2-digit",
                                                hourCycle: "h23",
                                            })}
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
        </section>
    );
}
