"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery } from "@/lib/api/query";
import type { components } from "@/lib/schema";
import { productionExport } from "@/lib/exports";
import { plantDay } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { CheckboxField, ControlField, useErrorMessage } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useCodeLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { ProductionDayNav } from "./history/day-nav";
import { IntervalEditor } from "./history/interval-editor";
import {
    daylightRows,
    energyValue,
    intervalKey,
    mostFrequentReason,
    saveFailure,
    type History,
    type Interval,
} from "./history/model";
import { ProposalDraft } from "./history/proposal-draft";
import { ProductionTable } from "./history/production-table";
import {
    canAct,
    controls,
    initialScreen,
    locked,
    reduceScreen,
    type Access,
    type ScreenEvent,
} from "./history/screen";
import { ProductionSourceDetails } from "./history/source-details";
import { ProductionImport } from "./production-import";

type ChangeRow = components["schemas"]["ProductionChangeRow"];
type Bounds = Pick<Interval, "starts_at" | "ends_at">;

export function ProductionHistory({
    scope,
    configuration,
    timezone,
    day: requestedDay,
    baseDay,
    onDay,
    onSaved,
}: {
    scope: PlantScope;
    configuration?: number;
    timezone: string;
    day?: string;
    baseDay: string | undefined;
    onDay: (day: string) => void;
    onSaved: () => Promise<unknown>;
}) {
    const t = useTranslations("ProductionHistory");
    const format = useFormat();
    const errorMessage = useErrorMessage();
    const reason = useCodeLabel("ProductionHistory.reasons");
    const canManage = useCanManageWorkspace();
    const proposals = useTranslations("ChangeRequests");
    const plant = useExportPlant();
    const [reconciled, setReconciled] = useState<History>();
    // The server builds days in the zone it answers with; the last answer bounds the day picker.
    const yesterday = plantDay(reconciled?.timezone ?? timezone, -1);
    const shown = (day?: string) => (day && day <= yesterday ? day : yesterday);
    const date = shown(requestedDay);
    // A step taken while a day change loads continues from the day being navigated to.
    const stepFrom = shown(baseDay);
    const access: Access = !configuration
        ? "none"
        : canManage
          ? "direct"
          : "propose";
    const [screen, setScreen] = useState(() => initialScreen(date, access));
    const [message, setMessage] = useState("");
    // Saves whose reload of these rows is still running; no export is built from rows being reloaded.
    const [reloads, setReloads] = useState(0);
    const editButtons = useRef(new Map<string, HTMLButtonElement>());
    const pendingFocus = useRef<string | null>(null);
    const summaryRef = useRef<HTMLDivElement>(null);
    const orphanCancel = useRef<HTMLButtonElement>(null);
    const { editor, draft } = screen;
    const reading = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/",
        {
            path: scope,
            query: { start_date: date, end_date: date, native_intervals: true },
        },
        { poll: editor.status === "closed" ? cadence.live : false },
    );
    const apply = (event: ScreenEvent) =>
        setScreen((current) => reduceScreen(current, event));
    if (screen.day !== date || screen.access !== access) {
        apply({ type: "dayShown", day: date });
        apply({ type: "accessChanged", access });
    }
    if (reading.data && reading.data !== reconciled) {
        const fresh = reading.data;
        setReconciled(fresh);
        apply({ type: "rowsLoaded", day: date, history: fresh });
    }
    const history = reading.data;
    const derived = controls(screen, history?.intervals);
    const drafting = draft !== null;
    useEffect(() => {
        if (!drafting) return;
        // A full page load drops the unsent draft, so the browser asks before leaving.
        const keep = (event: BeforeUnloadEvent) => event.preventDefault();
        window.addEventListener("beforeunload", keep);
        return () => window.removeEventListener("beforeunload", keep);
    }, [drafting]);
    useEffect(() => {
        if (editor.status !== "closed" || !history || !pendingFocus.current)
            return;
        const button = editButtons.current.get(pendingFocus.current);
        pendingFocus.current = null;
        if (button && !button.disabled) button.focus();
        else summaryRef.current?.focus();
    }, [editor.status, history]);
    useEffect(() => {
        const active = document.activeElement;
        // The editor's form is gone, so a focus lost with it moves to the editor's one control left.
        if (
            editor.status === "orphaned" &&
            (!active || active === document.body)
        )
            orphanCancel.current?.focus();
    }, [editor.status]);
    const rows = history?.intervals ?? [];
    const day = history?.days.find((day) => day.date === date);
    const daylight = daylightRows(rows, day?.daylight_windows ?? []);
    const visibleRows = screen.fullDay || !daylight.length ? rows : daylight;
    const hasValues = rows.some((row) => row.effective != null);
    const timeFormatter = format.intervals(
        history?.timezone ?? timezone,
        rows.flatMap((row) => [
            row.starts_at,
            row.ends_at,
            ...[
                ...row.provider_originals,
                ...row.previous_configuration_observations,
            ].flatMap((other) => [other.starts_at, other.ends_at]),
        ]),
    );
    const time = (value: string) => timeFormatter.format(new Date(value));
    const intervalLabel = (row: Bounds) =>
        `${time(row.starts_at)}–${time(row.ends_at)}`;
    const staged = new Set(
        draft && !derived.away ? draft.items.map((item) => item.key) : [],
    );
    const saving = locked(editor);

    // Applies an event decided on the shown state; null when the screen refuses it.
    function request(event: ScreenEvent) {
        const next = reduceScreen(screen, event);
        if (next === screen) return null;
        setScreen(next);
        return next;
    }
    async function reloadAfterSave() {
        setReloads((count) => count + 1);
        try {
            await onSaved();
        } finally {
            setReloads((count) => count - 1);
        }
    }
    function selectDate(value: string) {
        if (!value || value > yesterday || !derived.dayNav) return;
        onDay(value);
        setMessage("");
    }
    function cancel(key: string) {
        if (request({ type: "cancel" })) pendingFocus.current = key;
    }
    function stage(row: Interval, action: "set" | "revert") {
        const key = intervalKey(row);
        const next = request({ type: "stage", row, action });
        if (next?.editor.status !== "closed") return;
        pendingFocus.current = key;
        setMessage(
            proposals(staged.has(key) ? "stagedUpdated" : "staged", {
                interval: intervalLabel(row),
            }),
        );
    }
    function unstage(key: string) {
        const item = draft?.items.find((item) => item.key === key);
        const next = item && request({ type: "remove", key });
        if (!item || !next) return;
        setMessage(
            proposals("stagedRemoved", { interval: intervalLabel(item) }),
        );
        if (!next.draft) summaryRef.current?.focus();
    }
    function finish(text: string) {
        setMessage(text);
        summaryRef.current?.focus();
    }
    function editStaged(key: string) {
        const row = rows.find((row) => intervalKey(row) === key);
        // The editor opens in its row, so a row outside daylight brings the full day into view.
        if (row)
            request({ type: "open", row, reveal: !visibleRows.includes(row) });
    }
    async function reloadEditor() {
        const next = request({ type: "reload" });
        if (next?.editor.status !== "reloading") return;
        const { key } = next.editor;
        try {
            const updated = await reading.reload();
            const row = updated.intervals.find(
                (row) => intervalKey(row) === key,
            );
            apply({ type: "rowsLoaded", day: date, history: updated });
            if (row && canAct(access, row))
                setMessage(t("reloaded_draft_preserved"));
        } catch (error) {
            apply({ type: "reloadFailed", key, message: errorMessage(error) });
        }
    }
    async function save(
        row: Interval,
        action: "set" | "revert",
        confirmImplausible = false,
    ) {
        if (!configuration) return;
        const attempt = request({ type: "save", row, action })?.editor;
        if (attempt?.status !== "saving") return;
        const energy = energyValue(attempt.energy);
        setMessage("");
        try {
            const change: ChangeRow = {
                starts_at: row.starts_at,
                ends_at: row.ends_at,
                expected_revision_id: attempt.expectedRevision,
                action,
                // An edit on this screen is the operator's value; a revert ignores quality and source.
                ...(action === "set" && energy !== null
                    ? { energy, quality: attempt.quality, source: "operator" }
                    : { quality: "valid", source: "operator" }),
            };
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/changes/",
                {
                    path: scope,
                    body: {
                        configuration_id: configuration,
                        unit: "MWh",
                        changes: [change],
                        confirm_implausible: confirmImplausible,
                    },
                },
            );
            pendingFocus.current = attempt.key;
            apply({ type: "saved", key: attempt.key });
            setMessage(action === "revert" ? t("reverted") : t("saved"));
            await reloadAfterSave();
        } catch (error) {
            apply({
                type: "saveFailed",
                key: attempt.key,
                message: errorMessage(error),
                implausible: saveFailure(error).implausible,
            });
        }
    }
    const unavailable = (row: Interval | undefined) => {
        const code = canManage
            ? row?.edit_unavailable_reason
            : row?.proposal_unavailable_reason;
        return code ? reason(code, t("unavailable")) : t("unavailable");
    };
    const orphaned = {
        unavailable: t("interval_changed"),
        held: t("interval_held"),
        day_left: t("interval_day_left"),
    };
    const refreshButton = (
        <button
            className="button secondary production-refresh"
            type="button"
            aria-label={t("refresh")}
            disabled={!derived.refresh}
            onClick={reading.retry}
        >
            <span aria-hidden="true">↻</span>
        </button>
    );
    return (
        <section className="panel production-workspace">
            <h2 className="sr-only">{t("actuals")}</h2>
            <div className="production-controls">
                <ProductionDayNav
                    date={date}
                    stepFrom={stepFrom}
                    yesterday={yesterday}
                    disabled={!derived.dayNav}
                    onSelect={selectDate}
                />
                <CheckboxField
                    label={t("show_full_day")}
                    checked={screen.fullDay}
                    disabled={!derived.fullDay}
                    onChange={(on) => apply({ type: "fullDay", on })}
                />
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
                    <Notice error>{orphaned[editor.cause]}</Notice>
                    <ControlField
                        label={t("attempted_energy", {
                            interval: editor.key.split("/").map(time).join("–"),
                        })}
                    >
                        <input value={editor.energy} readOnly />
                    </ControlField>
                    <button
                        ref={orphanCancel}
                        className="button secondary"
                        type="button"
                        disabled={saving}
                        onClick={() => cancel(editor.key)}
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
                                        total: format.number(day.total_mwh),
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
                        <ProductionTable
                            tenantId={scope.tenant_id}
                            rows={rows}
                            visibleRows={visibleRows}
                            timeFormatter={timeFormatter}
                            intervalLabel={intervalLabel}
                            canManage={canManage}
                            canAct={(row) => canAct(access, row)}
                            hasConfiguration={Boolean(configuration)}
                            actionsDisabled={!derived.rowActions}
                            editingKey={
                                editor.status !== "closed" &&
                                editor.status !== "orphaned"
                                    ? editor.key
                                    : undefined
                            }
                            editButtons={editButtons}
                            unavailable={unavailable}
                            onOpen={(row) => request({ type: "open", row })}
                            renderEditor={(row) =>
                                editor.status !== "closed" && (
                                    <IntervalEditor
                                        row={row}
                                        editor={editor}
                                        interval={intervalLabel(row)}
                                        canManage={canManage}
                                        staged={staged.has(intervalKey(row))}
                                        saving={saving}
                                        onChange={(change) =>
                                            apply({ type: "change", ...change })
                                        }
                                        onSave={(action, confirmImplausible) =>
                                            canManage
                                                ? void save(
                                                      row,
                                                      action,
                                                      confirmImplausible,
                                                  )
                                                : stage(row, action)
                                        }
                                        onCancel={() =>
                                            cancel(intervalKey(row))
                                        }
                                        onReload={() => void reloadEditor()}
                                    />
                                )
                            }
                        />
                    )}
                    <CsvDownload
                        label={t("download_csv")}
                        pending={saving || reloads > 0}
                        build={() =>
                            productionExport(
                                { ...plant, timezone: history.timezone },
                                date,
                                rows,
                            )
                        }
                    />
                </>
            )}
            {configuration && draft && (
                <ProposalDraft
                    key={draft.day}
                    scope={scope}
                    configuration={configuration}
                    draft={draft}
                    controls={derived}
                    rows={history?.intervals}
                    intervalLabel={intervalLabel}
                    request={request}
                    onEvent={apply}
                    onEdit={editStaged}
                    onRemove={unstage}
                    onReturn={() => onDay(draft.day)}
                    onDiscard={() => {
                        if (request({ type: "discard" }))
                            finish(proposals("discarded"));
                    }}
                    onReload={reading.reload}
                    onSent={() => {
                        apply({ type: "sent" });
                        finish(proposals("submitted"));
                        void reloadAfterSave();
                    }}
                />
            )}
            <ProductionSourceDetails scope={scope} />
            {canManage && configuration && (
                <details className="production-import" id="production-import">
                    <summary role="button" aria-label={t("import_csv")}>
                        {t("import_csv")}
                    </summary>
                    <ProductionImport
                        scope={scope}
                        configuration={configuration}
                        timezone={timezone}
                        onSaved={() => void reloadAfterSave()}
                    />
                </details>
            )}
        </section>
    );
}
