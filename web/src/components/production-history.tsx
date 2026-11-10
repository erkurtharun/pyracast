"use client";
import { useEffect, useState } from "react";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { Notice } from "./ui";

type History = components["schemas"]["ProductionHistoryOutput"];
type Draft = { energy: string; unit: string };
const reasons: Record<string, string> = {
    configuration_unavailable: "No plant configuration for this interval.",
    configuration_not_current:
        "This interval belongs to an earlier configuration.",
    observation_overlap:
        "A saved reading overlaps this interval. See saved readings below.",
    estimated_actuals_unavailable:
        "No archived estimated actuals for this interval.",
    already_recorded: "Already saved.",
};
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
    const time = (value: string) =>
        new Intl.DateTimeFormat("en-GB", {
            timeZone: history?.timezone,
            hour: "2-digit",
            minute: "2-digit",
            timeZoneName: "short",
        }).format(new Date(value));
    const label = (start: string, end: string) =>
        `${time(start)} – ${time(end)}`;
    const datedTime = (value: string) =>
        new Intl.DateTimeFormat("en-GB", {
            timeZone: history?.timezone,
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            timeZoneName: "short",
        }).format(new Date(value));
    const day = ready ? history.days[0] : undefined;
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
            setMessage(
                `${submitted.length} meter ${submitted.length === 1 ? "reading" : "readings"} saved.`,
            );
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
                    <h2>Production journal</h2>
                    <p>Enter meter readings for one day at a time.</p>
                </div>
            </div>
            <p className="fine">
                Historical estimated actuals provide comparison data. Complete
                daylight coverage helps prepare a calibration candidate. Blank
                entries stay missing; saved readings are permanent.
            </p>
            <div className="journal-controls">
                <div className="journal-date-nav">
                    <button
                        type="button"
                        className="button secondary"
                        aria-label="Previous day"
                        disabled={!date}
                        onClick={() => setDate(shiftDay(date, -1))}
                    >
                        ←
                    </button>
                    <label className="field">
                        Day
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
                        aria-label="Next day"
                        disabled={!date || date >= (maxDate ?? "")}
                        onClick={() => setDate(shiftDay(date, 1))}
                    >
                        →
                    </button>
                </div>
                <label className="field">
                    Meter interval
                    <select
                        value={resolution}
                        onChange={(event) => setResolution(event.target.value)}
                    >
                        <option value="30">30 minutes</option>
                        <option value="60">60 minutes</option>
                    </select>
                </label>
                <label className="field">
                    Entry energy unit
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
                    Available comparison dates:{" "}
                    {history.available_start_date && history.available_end_date
                        ? `${history.available_start_date} to ${history.available_end_date}`
                        : "No archived estimated actuals yet"}
                    . Local times in {history.timezone}.
                </p>
            )}
            {error && <Notice error>{error}</Notice>}
            {message && <Notice>{message}</Notice>}
            {!ready ? (
                failedQuery === query ? null : (
                    <p role="status">Loading production history…</p>
                )
            ) : (
                <>
                    {day && (
                        <div className="journal-summary">
                            <div>
                                <strong>{day.recorded_count} recorded</strong>
                                <span>
                                    {day.missing_count} missing ·{" "}
                                    {day.available_count} with comparison data
                                </span>
                            </div>
                            <div>
                                <strong>Daylight</strong>
                                <span>
                                    {day.daylight_status === "polar_day"
                                        ? "Sun above horizon all day"
                                        : day.daylight_status === "polar_night"
                                          ? "No daylight this day"
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
                                                : "Daylight times unavailable"}
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
                        {history.intervals.length === 0 ? (
                            <p>No daylight intervals for this day.</p>
                        ) : (
                            <div className="table-scroll">
                                <table className="journal-table">
                                    <caption>
                                        Meter entries · {resolution} minutes
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>Local interval</th>
                                            <th>Production</th>
                                            <th>Status</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {history.intervals.map((row) => {
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
                                                            `${row.observation.energy_mwh} MWh`
                                                        ) : (
                                                            <>
                                                                <input
                                                                    className="meter-input"
                                                                    aria-label={`Production ${row.starts_at}`}
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
                                                              ? "Missing · ready for your reading"
                                                              : (reasons[
                                                                    row.unavailable_reason ??
                                                                        ""
                                                                ] ??
                                                                "Unavailable for entry.")}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        )}
                        <div className="journal-save">
                            <span>
                                {pending}{" "}
                                {pending === 1 ? "reading" : "readings"} ready
                                to save
                            </span>
                            <button
                                className="button"
                                disabled={saving || !pending}
                            >
                                {saving ? "Saving…" : "Save meter readings"}
                            </button>
                        </div>
                    </form>
                    {history.observations.length > 0 && (
                        <details>
                            <summary>
                                Saved meter readings (
                                {history.observations.length})
                            </summary>
                            <div className="table-scroll">
                                <table>
                                    <caption>
                                        Saved readings · original intervals and
                                        configuration
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>Local interval</th>
                                            <th>Energy (MWh)</th>
                                            <th>Quality</th>
                                            <th>Source</th>
                                            <th>Configuration</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {history.observations.map((row) => (
                                            <tr key={row.id}>
                                                <th>
                                                    {datedTime(row.starts_at)}{" "}
                                                    to {datedTime(row.ends_at)}
                                                </th>
                                                <td>{row.energy_mwh}</td>
                                                <td>{row.quality}</td>
                                                <td>{row.source}</td>
                                                <td>{row.configuration_id}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </details>
                    )}
                </>
            )}
        </section>
    );
}
