"use client";
import { useLocale, useTranslations } from "next-intl";
import { Fragment, useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery } from "@/lib/api/query";
import { useProposalKey } from "@/lib/idempotency";
import type { components } from "@/lib/schema";
import { dataIntervalRange, intervalTimeFormatter } from "@/lib/intervals";
import { QUALITIES, qualityOf, type Quality } from "@/lib/production";
import { CsvDownload } from "./csv-download";
import { localDay } from "./data-source";
import { ProductionImport } from "./production-import";
import { ScrollRegion } from "./scroll-region";
import { Notice } from "./ui";
import { useCanManageWorkspace } from "./workspace-access";

type History = components["schemas"]["ProductionHistoryOutput"];
type Interval = components["schemas"]["ProductionIntervalOutput"];
type ChangeRow = components["schemas"]["ProductionChangeRow"];

type Editor =
    | { status: "closed" }
    | {
          status: "editing" | "saving" | "reloading" | "failed" | "orphaned";
          key: string;
          energy: string;
          reason?: string;
          quality: Quality;
          expectedRevision: number | null;
          error?: string;
          pendingWrite?: boolean;
      };

function shiftDay(day: string, offset: number) {
    const date = new Date(`${day}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

function reconcileEditor(
    editor: Editor,
    history: History,
    unavailableMessage: string,
): Editor {
    if (editor.status === "closed" || editor.status === "orphaned")
        return editor;
    const row = history.intervals.find(
        (row) => `${row.starts_at}/${row.ends_at}` === editor.key,
    );
    if (!row || !(row.can_edit || row.can_propose))
        return {
            ...editor,
            status: "orphaned",
            error: unavailableMessage,
            pendingWrite: editor.status === "saving",
        };
    return editor.status === "reloading"
        ? {
              ...editor,
              status: "editing",
              expectedRevision: row.revision_id,
              error: undefined,
          }
        : editor;
}

function mostFrequentReason(rows: Interval[]) {
    const counts = new Map<string, number>();
    for (const row of rows) {
        const code = row.proposal_unavailable_reason;
        if (code) counts.set(code, (counts.get(code) ?? 0) + 1);
    }
    let best: Interval | undefined;
    let most = 0;
    for (const row of rows) {
        const count = counts.get(row.proposal_unavailable_reason ?? "") ?? 0;
        if (count > most) {
            best = row;
            most = count;
        }
    }
    return best;
}

function ProductionReading({
    row,
    timeFormatter,
}: {
    row: Interval;
    timeFormatter: Intl.DateTimeFormat;
}) {
    const t = useTranslations("ProductionHistory");
    const locale = useLocale();
    const number = (value: number) =>
        new Intl.NumberFormat(locale, { maximumFractionDigits: 3 }).format(
            value,
        );
    const time = (value: string) => timeFormatter.format(new Date(value));
    const corrected =
        row.observation != null || row.effective?.aggregated === true;
    const originals = row.provider_originals?.length
        ? row.provider_originals
        : row.provider_original
          ? [row.provider_original]
          : [];
    const source =
        row.effective?.source_kind === "settlement_injection"
            ? "settlement"
            : row.effective?.source_kind === "realtime_generation"
              ? "provisional"
              : "manual";
    const quality = row.effective?.quality;
    return (
        <>
            <div className="production-value">
                {row.effective == null
                    ? t("missing")
                    : number(row.effective.energy_mwh)}{" "}
                {corrected ? (
                    <span className="production-correction">
                        {t("corrected")}
                    </span>
                ) : (
                    row.effective && <span className="fine">{t(source)}</span>
                )}
            </div>
            {quality && quality !== "valid" && (
                <div className="fine">
                    {t.has(quality) ? t(quality) : quality}
                </div>
            )}
            {corrected &&
                (originals.length ? (
                    originals.map((original) => (
                        <div
                            className="production-original"
                            key={`${original.snapshot_id}/${original.starts_at}`}
                        >
                            {t(
                                originals.length > 1 ||
                                    Date.parse(original.starts_at) !==
                                        Date.parse(row.starts_at) ||
                                    Date.parse(original.ends_at) !==
                                        Date.parse(row.ends_at)
                                    ? "provider_original_interval"
                                    : "provider_original",
                                {
                                    interval: `${time(original.starts_at)}–${time(original.ends_at)}`,
                                    value: `${number(original.energy_mwh)} MWh`,
                                },
                            )}
                        </div>
                    ))
                ) : (
                    <div className="production-original">
                        {t("provider_original", { value: t("missing") })}
                    </div>
                ))}
        </>
    );
}

export function ProductionHistory({
    scope,
    configuration,
    timezone,
    day: requestedDay,
    onDay,
    onSaved,
}: {
    scope: PlantScope;
    configuration?: number;
    timezone: string;
    day?: string;
    onDay: (day: string) => void;
    onSaved: () => Promise<unknown>;
}) {
    const t = useTranslations("ProductionHistory");
    const locale = useLocale();
    const canManage = useCanManageWorkspace();
    const proposals = useTranslations("ChangeRequests");
    const proposalKey = useProposalKey();
    const yesterday = localDay(timezone, -1);
    const date =
        requestedDay && requestedDay <= yesterday ? requestedDay : yesterday;
    const [showFullDay, setShowFullDay] = useState(false);
    const [editor, setEditor] = useState<Editor>({ status: "closed" });
    const [message, setMessage] = useState("");
    const editButtons = useRef(new Map<string, HTMLButtonElement>());
    const pendingFocus = useRef<string | null>(null);
    const summaryRef = useRef<HTMLDivElement>(null);
    const connection = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production-source/",
        { path: scope },
    );
    const reading = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/",
        {
            path: scope,
            query: { start_date: date, end_date: date, native_intervals: true },
        },
        { poll: editor.status === "closed" ? cadence.live : false },
    );
    const [reconciledAt, setReconciledAt] = useState(0);
    if (reading.data && reading.updatedAt !== reconciledAt) {
        const fresh = reading.data;
        setReconciledAt(reading.updatedAt);
        setEditor((previous) =>
            reconcileEditor(previous, fresh, t("interval_changed")),
        );
    }
    const history = reading.data;
    useEffect(() => {
        if (editor.status !== "closed" || !history || !pendingFocus.current)
            return;
        const button = editButtons.current.get(pendingFocus.current);
        pendingFocus.current = null;
        if (button && !button.disabled) button.focus();
        else summaryRef.current?.focus();
    }, [editor.status, history]);
    const rows = history?.intervals ?? [];
    const generationRows = dataIntervalRange(
        rows,
        (row) => row.effective != null && row.effective.energy_mwh > 0,
    );
    const visibleRows =
        showFullDay || !generationRows.length ? rows : generationRows;
    const day = history?.days.find((day) => day.date === date);
    const hasValues = rows.some((row) => row.effective != null);
    const number = (value: number) =>
        new Intl.NumberFormat(locale, { maximumFractionDigits: 3 }).format(
            value,
        );
    const timeFormatter = intervalTimeFormatter(
        locale,
        history?.timezone ?? timezone,
        rows.flatMap((row) => [
            row.starts_at,
            row.ends_at,
            ...(
                row.provider_originals ??
                (row.provider_original ? [row.provider_original] : [])
            ).flatMap((original) => [original.starts_at, original.ends_at]),
        ]),
    );
    const time = (value: string) => timeFormatter.format(new Date(value));
    const intervalLabel = (row: Interval) =>
        `${time(row.starts_at)}–${time(row.ends_at)}`;
    const key = (row: Interval) => `${row.starts_at}/${row.ends_at}`;
    const qualityLabel = (quality: string) =>
        ({
            valid: t("valid"),
            invalid: t("invalid"),
            outage: t("outage"),
            curtailed: t("curtailed"),
            mixed: t("mixed"),
        })[quality] ?? quality;
    const saving =
        editor.status === "saving" ||
        editor.status === "reloading" ||
        (editor.status === "orphaned" && editor.pendingWrite === true);

    function selectDate(value: string) {
        if (!value || value > yesterday || saving) return;
        onDay(value);
        setEditor({ status: "closed" });
        setMessage("");
    }
    async function reloadEditor() {
        if (editor.status === "closed" || saving) return;
        const attempt = editor;
        setEditor({ ...attempt, status: "reloading" });
        try {
            const updated = await reading.reload();
            const row = updated.intervals.find(
                (row) => key(row) === attempt.key,
            );
            if (row && (canManage ? row.can_edit : row.can_propose)) {
                setEditor({
                    ...attempt,
                    status: "editing",
                    expectedRevision: row.revision_id,
                    error: undefined,
                });
                setMessage(t("reloaded_draft_preserved"));
            } else {
                setEditor({
                    ...attempt,
                    status: "orphaned",
                    error: t("interval_changed"),
                });
            }
        } catch (error) {
            setEditor((current) =>
                current.status === "orphaned" && current.key === attempt.key
                    ? current
                    : {
                          ...attempt,
                          status: "failed",
                          error: (error as Error).message,
                      },
            );
        }
    }
    async function save(row: Interval, action: "set" | "revert") {
        if (
            !configuration ||
            !(canManage || row.can_propose) ||
            saving ||
            !history ||
            !(canManage ? row.can_edit : row.can_propose) ||
            (action === "revert" &&
                !(canManage ? row.can_revert : row.can_propose_revert))
        )
            return;
        const attempt =
            editor.status !== "closed" && editor.key === key(row)
                ? editor
                : {
                      key: key(row),
                      energy: row.effective?.energy_mwh.toString() ?? "",
                      quality: qualityOf(row.observation?.quality),
                      expectedRevision: row.revision_id,
                      reason: "",
                  };
        const energy = Number(attempt.energy.trim().replace(",", "."));
        if (
            action === "set" &&
            (!attempt.energy.trim() || !Number.isFinite(energy) || energy < 0)
        ) {
            setEditor({
                ...attempt,
                status: "failed",
                error: t("invalid_energy"),
            });
            return;
        }
        if (!canManage && !attempt.reason?.trim()) {
            setEditor({
                ...attempt,
                status: "failed",
                error: proposals("reasonRequired"),
            });
            return;
        }
        setEditor({ ...attempt, status: "saving" });
        setMessage("");
        try {
            const change: ChangeRow = {
                starts_at: row.starts_at,
                ends_at: row.ends_at,
                expected_revision_id: attempt.expectedRevision,
                action,
                ...(action === "set"
                    ? {
                          energy,
                          quality: attempt.quality,
                          source: row.observation?.source ?? "meter",
                      }
                    : // A revert ignores quality and source; these are the server defaults.
                      { quality: "valid", source: "operator" }),
            };
            const payload = {
                configuration_id: configuration,
                unit: "MWh" as const,
                changes: [change],
            };
            if (canManage) {
                await api.post(
                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/changes/",
                    {
                        path: scope,
                        body: payload,
                    },
                );
            } else {
                const proposal = {
                    ...payload,
                    reason: attempt.reason?.trim() ?? "",
                };
                await api.post(
                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/change-requests/production/",
                    {
                        path: scope,
                        body: {
                            ...proposal,
                            idempotency_key: proposalKey.forPayload(proposal),
                        },
                    },
                );
                proposalKey.reset();
            }
            pendingFocus.current = attempt.key;
            setEditor({ status: "closed" });
            setMessage(
                canManage
                    ? action === "revert"
                        ? t("reverted")
                        : t("saved")
                    : proposals("submitted"),
            );
            await onSaved();
        } catch (error) {
            setEditor((current) =>
                current.status === "orphaned" && current.key === attempt.key
                    ? { ...current, pendingWrite: false }
                    : {
                          ...attempt,
                          status: "failed",
                          error: (error as Error).message,
                      },
            );
        }
    }
    const unavailable = (row: Interval | undefined) => {
        const code = canManage
            ? row?.edit_unavailable_reason
            : row?.proposal_unavailable_reason;
        return code && t.has(`reasons.${code}`)
            ? t(`reasons.${code}`)
            : t("unavailable");
    };
    const refreshButton = (
        <button
            className="button secondary production-refresh"
            type="button"
            aria-label={t("refresh")}
            disabled={saving || editor.status !== "closed"}
            onClick={reading.retry}
        >
            <span aria-hidden="true">↻</span>
        </button>
    );
    return (
        <section className="panel production-workspace">
            <h2 className="sr-only">{t("actuals")}</h2>
            <div className="production-controls">
                <div className="archive-date-nav">
                    <button
                        className="button secondary"
                        type="button"
                        aria-label={t("previous_day")}
                        disabled={saving}
                        onClick={() => selectDate(shiftDay(date, -1))}
                    >
                        ←
                    </button>
                    <label className="field">
                        {t("day")}
                        <input
                            type="date"
                            value={date}
                            max={yesterday}
                            disabled={saving}
                            onChange={(event) => selectDate(event.target.value)}
                        />
                    </label>
                    <button
                        className="button secondary"
                        type="button"
                        aria-label={t("next_day")}
                        disabled={saving || date >= yesterday}
                        onClick={() => selectDate(shiftDay(date, 1))}
                    >
                        →
                    </button>
                </div>
                <label className="field">
                    <span>
                        <input
                            type="checkbox"
                            checked={showFullDay}
                            disabled={editor.status !== "closed"}
                            onChange={(event) =>
                                setShowFullDay(event.target.checked)
                            }
                        />{" "}
                        {t("show_full_day")}
                    </span>
                </label>
                {!history && refreshButton}
            </div>
            {message && <Notice>{message}</Notice>}
            {history && reading.error && (
                <Notice error>
                    {t("refresh_failed", { error: reading.error })}
                </Notice>
            )}
            {editor.status === "orphaned" && (
                <div className="production-row-editor">
                    <Notice error>{editor.error}</Notice>
                    <label className="field">
                        {t("attempted_energy", {
                            interval: editor.key.split("/").map(time).join("–"),
                        })}
                        <input value={editor.energy} readOnly />
                    </label>
                    <button
                        className="button secondary"
                        type="button"
                        disabled={saving}
                        onClick={() => {
                            pendingFocus.current = editor.key;
                            setEditor({ status: "closed" });
                        }}
                    >
                        {t("cancel")}
                    </button>
                </div>
            )}
            {reading.loading ? (
                <p role="status">{t("loading_production_history")}</p>
            ) : !history ? (
                <Notice error>{reading.error}</Notice>
            ) : (
                history && (
                    <>
                        <div
                            className="production-summary"
                            role="group"
                            aria-label={t("production_intervals")}
                            tabIndex={-1}
                            ref={summaryRef}
                        >
                            <strong>
                                {!hasValues
                                    ? t("no_values")
                                    : day?.total_mwh == null
                                      ? t("total_unavailable")
                                      : t("total", {
                                            total: number(day.total_mwh),
                                        })}
                            </strong>
                            {refreshButton}
                            {rows.some((row) => row.effective == null) && (
                                <span>
                                    {t("missing_intervals", {
                                        count: rows.filter(
                                            (row) => row.effective == null,
                                        ).length,
                                    })}
                                </span>
                            )}
                        </div>
                        {!canManage &&
                            rows.length > 0 &&
                            !rows.some((row) => row.can_propose) && (
                                <Notice>
                                    {t("none_available", {
                                        reason: unavailable(
                                            mostFrequentReason(rows),
                                        ),
                                    })}
                                </Notice>
                            )}
                        {!rows.length ? (
                            <p>{t("no_intervals")}</p>
                        ) : (
                            <ScrollRegion label={t("production_intervals")}>
                                <table className="production-table">
                                    <caption>
                                        {t("production_intervals")} ·{" "}
                                        {history.timezone}
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>{t("local_interval")}</th>
                                            <th>{t("production_mwh")}</th>
                                            {(canManage ||
                                                rows.some(
                                                    (row) => row.can_propose,
                                                )) && <th>{t("actions")}</th>}
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {visibleRows.map((row) => {
                                            const editing =
                                                editor.status !== "closed" &&
                                                editor.status !== "orphaned" &&
                                                editor.key === key(row);
                                            return (
                                                <Fragment key={key(row)}>
                                                    <tr>
                                                        <th scope="row">
                                                            {intervalLabel(row)}
                                                        </th>
                                                        <td>
                                                            <ProductionReading
                                                                row={row}
                                                                timeFormatter={
                                                                    timeFormatter
                                                                }
                                                            />
                                                        </td>
                                                        {(canManage ||
                                                            rows.some(
                                                                (row) =>
                                                                    (
                                                                        row as Interval
                                                                    )
                                                                        .can_propose,
                                                            )) && (
                                                            <td>
                                                                {(canManage
                                                                    ? row.can_edit
                                                                    : (
                                                                          row as Interval
                                                                      )
                                                                          .can_propose) &&
                                                                configuration ? (
                                                                    <button
                                                                        ref={(
                                                                            button,
                                                                        ) => {
                                                                            if (
                                                                                button
                                                                            )
                                                                                editButtons.current.set(
                                                                                    key(
                                                                                        row,
                                                                                    ),
                                                                                    button,
                                                                                );
                                                                            else
                                                                                editButtons.current.delete(
                                                                                    key(
                                                                                        row,
                                                                                    ),
                                                                                );
                                                                        }}
                                                                        className="button secondary"
                                                                        type="button"
                                                                        disabled={
                                                                            editor.status !==
                                                                            "closed"
                                                                        }
                                                                        aria-label={t(
                                                                            canManage
                                                                                ? "edit_interval"
                                                                                : "propose_interval",
                                                                            {
                                                                                interval:
                                                                                    intervalLabel(
                                                                                        row,
                                                                                    ),
                                                                            },
                                                                        )}
                                                                        onClick={() =>
                                                                            setEditor(
                                                                                {
                                                                                    status: "editing",
                                                                                    key: key(
                                                                                        row,
                                                                                    ),
                                                                                    energy:
                                                                                        row.effective?.energy_mwh.toString() ??
                                                                                        "",
                                                                                    quality:
                                                                                        row
                                                                                            .observation
                                                                                            ?.quality ??
                                                                                        "valid",
                                                                                    expectedRevision:
                                                                                        row.revision_id,
                                                                                },
                                                                            )
                                                                        }
                                                                    >
                                                                        {canManage
                                                                            ? t(
                                                                                  "edit",
                                                                              )
                                                                            : proposals(
                                                                                  "proposeCorrection",
                                                                              )}
                                                                    </button>
                                                                ) : (
                                                                    <span className="fine">
                                                                        {unavailable(
                                                                            row,
                                                                        )}
                                                                    </span>
                                                                )}
                                                            </td>
                                                        )}
                                                    </tr>
                                                    {editing && (
                                                        <tr>
                                                            <td
                                                                colSpan={
                                                                    canManage ||
                                                                    rows.some(
                                                                        (row) =>
                                                                            (
                                                                                row as Interval
                                                                            )
                                                                                .can_propose,
                                                                    )
                                                                        ? 3
                                                                        : 2
                                                                }
                                                            >
                                                                <form
                                                                    className="production-row-editor"
                                                                    onSubmit={(
                                                                        event,
                                                                    ) => {
                                                                        event.preventDefault();
                                                                        void save(
                                                                            row,
                                                                            "set",
                                                                        );
                                                                    }}
                                                                >
                                                                    <label className="field">
                                                                        {t(
                                                                            "edit_energy",
                                                                            {
                                                                                interval:
                                                                                    intervalLabel(
                                                                                        row,
                                                                                    ),
                                                                            },
                                                                        )}
                                                                        <input
                                                                            inputMode="decimal"
                                                                            autoFocus
                                                                            value={
                                                                                editor.energy
                                                                            }
                                                                            disabled={
                                                                                saving
                                                                            }
                                                                            onChange={(
                                                                                event,
                                                                            ) =>
                                                                                setEditor(
                                                                                    {
                                                                                        ...editor,
                                                                                        status: "editing",
                                                                                        energy: event
                                                                                            .target
                                                                                            .value,
                                                                                        error: undefined,
                                                                                    },
                                                                                )
                                                                            }
                                                                        />
                                                                    </label>
                                                                    <label className="field">
                                                                        {t(
                                                                            "quality",
                                                                        )}
                                                                        <select
                                                                            value={
                                                                                editor.quality
                                                                            }
                                                                            disabled={
                                                                                saving
                                                                            }
                                                                            onChange={(
                                                                                event,
                                                                            ) =>
                                                                                setEditor(
                                                                                    {
                                                                                        ...editor,
                                                                                        status: "editing",
                                                                                        quality:
                                                                                            qualityOf(
                                                                                                event
                                                                                                    .target
                                                                                                    .value,
                                                                                            ),
                                                                                        error: undefined,
                                                                                    },
                                                                                )
                                                                            }
                                                                        >
                                                                            {QUALITIES.map(
                                                                                (
                                                                                    quality,
                                                                                ) => (
                                                                                    <option
                                                                                        key={
                                                                                            quality
                                                                                        }
                                                                                        value={
                                                                                            quality
                                                                                        }
                                                                                    >
                                                                                        {qualityLabel(
                                                                                            quality,
                                                                                        )}
                                                                                    </option>
                                                                                ),
                                                                            )}
                                                                        </select>
                                                                    </label>
                                                                    {!canManage && (
                                                                        <label className="field">
                                                                            {proposals(
                                                                                "reason",
                                                                            )}
                                                                            <textarea
                                                                                required
                                                                                maxLength={
                                                                                    500
                                                                                }
                                                                                value={
                                                                                    editor.reason ??
                                                                                    ""
                                                                                }
                                                                                disabled={
                                                                                    saving
                                                                                }
                                                                                onChange={(
                                                                                    event,
                                                                                ) =>
                                                                                    setEditor(
                                                                                        {
                                                                                            ...editor,
                                                                                            status: "editing",
                                                                                            reason: event
                                                                                                .target
                                                                                                .value,
                                                                                            error: undefined,
                                                                                        },
                                                                                    )
                                                                                }
                                                                            />
                                                                            <span className="field-hint">
                                                                                {proposals(
                                                                                    "approvalHelp",
                                                                                )}
                                                                            </span>
                                                                        </label>
                                                                    )}
                                                                    <div className="button-group">
                                                                        <button
                                                                            className="button"
                                                                            type="submit"
                                                                            disabled={
                                                                                saving ||
                                                                                (canManage &&
                                                                                    editor.status ===
                                                                                        "failed")
                                                                            }
                                                                        >
                                                                            {saving
                                                                                ? t(
                                                                                      "saving",
                                                                                  )
                                                                                : canManage
                                                                                  ? t(
                                                                                        "save",
                                                                                    )
                                                                                  : proposals(
                                                                                        "send",
                                                                                    )}
                                                                        </button>
                                                                        <button
                                                                            className="button secondary"
                                                                            type="button"
                                                                            disabled={
                                                                                saving
                                                                            }
                                                                            onClick={() => {
                                                                                pendingFocus.current =
                                                                                    key(
                                                                                        row,
                                                                                    );
                                                                                proposalKey.reset();
                                                                                setEditor(
                                                                                    {
                                                                                        status: "closed",
                                                                                    },
                                                                                );
                                                                            }}
                                                                        >
                                                                            {t(
                                                                                "cancel",
                                                                            )}
                                                                        </button>
                                                                    </div>
                                                                    {(canManage
                                                                        ? row.can_revert
                                                                        : (
                                                                              row as Interval
                                                                          )
                                                                              .can_propose_revert) && (
                                                                        <button
                                                                            className="button secondary"
                                                                            type="button"
                                                                            disabled={
                                                                                saving ||
                                                                                (canManage &&
                                                                                    editor.status ===
                                                                                        "failed")
                                                                            }
                                                                            aria-label={
                                                                                canManage
                                                                                    ? t(
                                                                                          "revert_interval",
                                                                                          {
                                                                                              interval:
                                                                                                  intervalLabel(
                                                                                                      row,
                                                                                                  ),
                                                                                          },
                                                                                      )
                                                                                    : proposals(
                                                                                          "proposeRevertInterval",
                                                                                          {
                                                                                              interval:
                                                                                                  intervalLabel(
                                                                                                      row,
                                                                                                  ),
                                                                                          },
                                                                                      )
                                                                            }
                                                                            onClick={() =>
                                                                                void save(
                                                                                    row,
                                                                                    "revert",
                                                                                )
                                                                            }
                                                                        >
                                                                            {canManage
                                                                                ? t(
                                                                                      "use_provider",
                                                                                  )
                                                                                : proposals(
                                                                                      "proposeRevert",
                                                                                  )}
                                                                        </button>
                                                                    )}
                                                                    {editor.status ===
                                                                        "failed" && (
                                                                        <>
                                                                            <Notice
                                                                                error
                                                                            >
                                                                                {
                                                                                    editor.error
                                                                                }
                                                                            </Notice>
                                                                            <button
                                                                                className="button secondary"
                                                                                type="button"
                                                                                onClick={() =>
                                                                                    void reloadEditor()
                                                                                }
                                                                            >
                                                                                {t(
                                                                                    "reload_values",
                                                                                )}
                                                                            </button>
                                                                        </>
                                                                    )}
                                                                </form>
                                                            </td>
                                                        </tr>
                                                    )}
                                                </Fragment>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </ScrollRegion>
                        )}
                        <CsvDownload
                            label={t("download_csv")}
                            filename={`production-${date}`}
                            records={rows.map((row) => {
                                const originals = row.provider_originals?.length
                                    ? row.provider_originals
                                    : row.provider_original
                                      ? [row.provider_original]
                                      : [];
                                return {
                                    starts_at: row.starts_at,
                                    ends_at: row.ends_at,
                                    energy: row.effective?.energy_mwh ?? null,
                                    quality: row.effective?.quality ?? "",
                                    source: row.effective?.source ?? "",
                                    provider_energy_mwh: originals
                                        .map((original) => original.energy_mwh)
                                        .join("; "),
                                    provider_intervals: originals
                                        .map(
                                            (original) =>
                                                `${original.starts_at}/${original.ends_at}`,
                                        )
                                        .join("; "),
                                    provider_source: originals
                                        .map((original) => original.source_kind)
                                        .join("; "),
                                };
                            })}
                            metadata={{
                                timezone: history.timezone,
                                unit: "MWh",
                            }}
                        />
                    </>
                )
            )}
            <details className="production-source">
                <summary>{t("source_details")}</summary>
                <p>{t("source_policy")}</p>
                {connection.loading ? (
                    <p role="status">{t("loading_source")}</p>
                ) : connection.error ? (
                    <Notice error>{connection.error}</Notice>
                ) : (
                    connection.data && (
                        <>
                            <p>
                                <strong>
                                    {connection.data.name || "EPİAŞ"}
                                </strong>{" "}
                                ·{" "}
                                {connection.data.connected
                                    ? t("connected")
                                    : t("not_connected")}
                            </p>
                            {!connection.data.connected && (
                                <p>{t("source_disconnected")}</p>
                            )}
                            <p className="fine">
                                {t("archived_days", {
                                    count: connection.data.archived_days,
                                })}
                            </p>
                            {connection.data.connected &&
                                connection.data.last_error && (
                                    <Notice error>
                                        {connection.data.last_error}
                                    </Notice>
                                )}
                        </>
                    )
                )}
                <p className="fine">{t("correction_policy")}</p>
            </details>
            {canManage && configuration && (
                <details className="production-import" id="production-import">
                    <summary role="button" aria-label={t("import_csv")}>
                        {t("import_csv")}
                    </summary>
                    <ProductionImport
                        scope={scope}
                        configuration={configuration}
                        timezone={timezone}
                        onSaved={onSaved}
                    />
                </details>
            )}
        </section>
    );
}
