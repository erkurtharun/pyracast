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
    const [range, setRange] = useState({
        start: "",
        end: "",
        resolution: "60",
    });
    const [history, setHistory] = useState<History>();
    const [loadedQuery, setLoadedQuery] = useState("");
    const [failedQuery, setFailedQuery] = useState("");
    const [version, setVersion] = useState(0);
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [saving, setSaving] = useState(false);
    const [unit, setUnit] = useState("kWh");
    const [drafts, setDrafts] = useState<Record<string, Draft>>({});
    const query = `${base}production/?resolution_minutes=${range.resolution}${range.start && range.end ? `&start_date=${range.start}&end_date=${range.end}` : ""}`;
    useEffect(() => {
        let current = true;
        request<History>(query)
            .then((data) => {
                if (!current) return;
                setHistory(data);
                setLoadedQuery(query);
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
    const label = (start: string, end: string) => {
        const format = new Intl.DateTimeFormat("en-GB", {
            timeZone: history?.timezone,
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        });
        return `${format.format(new Date(start))} – ${format.format(new Date(end))}`;
    };
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
            setMessage(`${submitted.length} meter readings saved.`);
            setVersion((current) => current + 1);
            onSaved();
        } catch (failure) {
            setError((failure as Error).message);
        } finally {
            setSaving(false);
        }
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Production journal</h2>
                    <p>
                        See saved meter readings and fill missing production
                        whenever you are ready.
                    </p>
                </div>
            </div>
            <p>
                Historical estimated actuals provide the comparison data. Once
                at least 7 complete, valid paired days are available, a fit is
                prepared automatically in the background. You review and
                activate any candidate below.
            </p>
            {history && (
                <p className="fine">
                    Available comparison dates:{" "}
                    {history.available_start_date && history.available_end_date
                        ? `${history.available_start_date} to ${history.available_end_date}`
                        : "No archived estimated actuals yet"}
                    . This window grows as daily data is collected. Local times
                    in {history.timezone}.
                </p>
            )}
            <div className="forecast-controls">
                <label className="field">
                    From date
                    <input
                        type="date"
                        value={range.start || history?.start_date || ""}
                        max={
                            history ? localDay(history.timezone, -1) : undefined
                        }
                        onChange={(event) =>
                            setRange({
                                ...range,
                                start: event.target.value,
                                end: range.end || history?.end_date || "",
                            })
                        }
                    />
                </label>
                <label className="field">
                    To date
                    <input
                        type="date"
                        value={range.end || history?.end_date || ""}
                        max={
                            history ? localDay(history.timezone, -1) : undefined
                        }
                        onChange={(event) =>
                            setRange({
                                ...range,
                                start: range.start || history?.start_date || "",
                                end: event.target.value,
                            })
                        }
                    />
                </label>
                <label className="field">
                    Meter interval
                    <select
                        value={range.resolution}
                        onChange={(event) =>
                            setRange({
                                ...range,
                                resolution: event.target.value,
                            })
                        }
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
            <p className="fine">
                View up to 31 days at a time. Blank entries stay missing. Saved
                readings are permanent; only intervals with archived comparison
                data accept new entries.
            </p>
            {error && <Notice error>{error}</Notice>}
            {message && <Notice>{message}</Notice>}
            {!ready ? (
                failedQuery === query ? null : (
                    <p role="status">Loading production history…</p>
                )
            ) : (
                <>
                    <div className="table-scroll">
                        <table>
                            <caption>Daily coverage</caption>
                            <thead>
                                <tr>
                                    <th>Local date</th>
                                    <th>Available intervals</th>
                                    <th>Recorded</th>
                                    <th>Missing</th>
                                </tr>
                            </thead>
                            <tbody>
                                {history.days.map((day) => (
                                    <tr key={day.date}>
                                        <th>{day.date}</th>
                                        <td>{day.available_count}</td>
                                        <td>{day.recorded_count}</td>
                                        <td>{day.missing_count}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <form
                        onSubmit={(event) => {
                            event.preventDefault();
                            void save();
                        }}
                    >
                        <div className="table-scroll">
                            <table>
                                <caption>
                                    Meter entries · {range.resolution} minutes
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
                                                <th>
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
                        <button
                            className="button"
                            disabled={
                                saving ||
                                !history.intervals.some(
                                    (row) =>
                                        row.can_enter &&
                                        drafts[
                                            `${row.starts_at}/${row.ends_at}`
                                        ]?.energy.trim(),
                                )
                            }
                        >
                            {saving ? "Saving…" : "Save meter readings"}
                        </button>
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
                                                    {label(
                                                        row.starts_at,
                                                        row.ends_at,
                                                    )}
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
