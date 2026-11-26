"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { UnconfirmedError } from "@/lib/api/errors";
import { cadence, useApiQuery } from "@/lib/api/query";
import { productionExport } from "@/lib/exports";
import { plantDay } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { useLeaveGuard } from "@/lib/leave-guard";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import {
    CheckboxField,
    ControlField,
    UnconfirmedNotice,
    WriteFailure,
} from "@/ui/form";
import { Notice, ReadFailed } from "@/ui/ui";
import { useCodeLabel, useLabel, useReceipt } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { ProductionDayNav } from "./history/day-nav";
import { IntervalEditor } from "./history/interval-editor";
import {
    daylightRows,
    intervalKey,
    mostFrequentReason,
    saveFailure,
    type History,
    type Interval,
    type Write,
} from "./history/model";
import { ProposalDraft } from "./history/proposal-draft";
import { ProductionTable } from "./history/production-table";
import {
    canAct,
    controls,
    dropped,
    editorUnsaved,
    initialScreen,
    locked,
    reduceScreen,
    repeatsAttempt,
    repeatsOrphan,
    unsentInput,
    type Access,
    type ScreenEvent,
} from "./history/screen";
import { ProductionSourceDetails } from "./history/source-details";
import { ProductionImport } from "./production-import";

type Bounds = Pick<Interval, "starts_at" | "ends_at">;

export type ShownDay = { date: string; unsent: boolean };

export function ProductionHistory({
    scope,
    configuration,
    timezone,
    day: requestedDay,
    baseDay,
    onDay,
    onKeepDay,
    onShown,
    onSaved,
}: {
    scope: PlantScope;
    configuration?: number;
    timezone: string;
    day?: string;
    baseDay: string | undefined;
    onDay: (day: string) => void;
    onKeepDay: (day: string) => void;
    onShown: (shown: ShownDay | null) => void;
    onSaved: () => Promise<unknown>;
}) {
    const t = useTranslations("ProductionHistory");
    const format = useFormat();
    const receipt = useReceipt();
    const reason = useCodeLabel("ProductionHistory.reasons");
    const label = useLabel();
    const canManage = useCanManageWorkspace();
    const proposals = useTranslations("ChangeRequests");
    const plant = useExportPlant();
    const [reconciled, setReconciled] = useState<{
        history: History;
        at: number;
    }>();
    // The server builds days in the zone it answers with; the last answer bounds the day picker.
    const yesterday = plantDay(reconciled?.history.timezone ?? timezone, -1);
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
    // Each message replaces the spoken node, so a message said again is announced again.
    const [message, setSaid] = useState({ text: "", count: 0 });
    const setMessage = (text: string) =>
        setSaid((said) => ({ text, count: said.count + 1 }));
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
        const shownDay = reduceScreen(screen, { type: "dayShown", day: date });
        const next = reduceScreen(shownDay, { type: "accessChanged", access });
        setScreen(next);
        // An access change replaces the notice of an earlier one, or clears it when it dropped nothing.
        if (screen.access !== access)
            setMessage(
                dropped(shownDay, next) ? t("access_dropped", { access }) : "",
            );
    }
    // A read answering the same rows keeps their object, so each answered read is told by its time.
    if (
        reading.data &&
        (reading.data !== reconciled?.history ||
            reading.updatedAt !== reconciled.at)
    ) {
        const fresh = reading.data;
        setReconciled({ history: fresh, at: reading.updatedAt });
        apply({ type: "rowsLoaded", day: date, history: fresh });
    }
    const history = reading.data;
    const derived = controls(screen, history?.intervals);
    useLeaveGuard(draft !== null || editorUnsaved(editor));
    const unsent = unsentInput(screen);
    useEffect(() => {
        onShown({ date, unsent });
        return () => onShown(null);
    }, [onShown, date, unsent]);
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
    const editing =
        editor.status !== "closed" && editor.status !== "orphaned"
            ? editor.key
            : undefined;
    // The open editor's row stays in view while it exists, also once a read moved it out of daylight.
    const visibleRows =
        screen.fullDay || !daylight.length
            ? rows
            : rows.filter(
                  (row) =>
                      daylight.includes(row) || intervalKey(row) === editing,
              );
    const hasValues = rows.some((row) => row.effective != null);
    const time = format.intervals(
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
    const intervalLabel = (row: Bounds) =>
        `${time(row.starts_at)}–${time(row.ends_at)}`;
    const staged = new Set(
        draft && !derived.away ? draft.items.map((item) => item.key) : [],
    );
    const saving = locked(editor);
    // An orphaned editor's repeat sends its retained write, which can differ from the entry it shows.
    const retained = (write: Write) =>
        write.action === "revert"
            ? t("remove_correction")
            : t("save_value", {
                  energy: format.number(write.change.energy ?? 0, 6),
                  quality: label("ObservationQuality", write.change.quality),
              });
    const orphanRepeats = repeatsOrphan(screen);
    const datedInterval = (key: string) => {
        const [start = "", end = ""] = key.split("/");
        const date = format.date(start, history?.timezone ?? timezone);
        return `${date}, ${time(start)}–${time(end)}`;
    };

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
        const next = request({ type: "stage", row, action, rows });
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
        setMessage("");
        try {
            const updated = await reading.reload();
            apply({ type: "rowsLoaded", day: date, history: updated });
        } catch (error) {
            apply({ type: "reloadFailed", key, error });
        }
    }
    function save(
        row: Interval,
        action: "set" | "revert",
        confirmImplausible = false,
    ) {
        const sending = request({
            type: "save",
            row,
            action,
            confirmImplausible,
        })?.editor;
        if (sending?.status === "saving")
            void deliver(sending.write, sending.key);
    }
    function repeatOrphan() {
        const sending = request({ type: "repeat" })?.editor;
        if (sending?.status === "orphaned" && sending.write)
            void deliver(sending.write, sending.key);
    }
    async function deliver(write: Write, key: string) {
        if (!configuration) return;
        setMessage("");
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/changes/",
                {
                    path: scope,
                    body: {
                        configuration_id: configuration,
                        unit: "MWh",
                        changes: [write.change],
                        confirm_implausible: write.confirmImplausible,
                    },
                },
            );
            pendingFocus.current = key;
            apply({ type: "saved" });
            setMessage(write.action === "revert" ? t("reverted") : t("saved"));
            await reloadAfterSave();
        } catch (error) {
            apply({
                type: "saveFailed",
                error,
                implausible: saveFailure(error).implausible,
            });
            if (error instanceof UnconfirmedError)
                void reading.reload().catch(() => undefined);
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
    // The day_left text speaks of the write still running; once it failed, the editor says it was not saved and why.
    const orphanNotice = (
        orphan: Extract<typeof editor, { status: "orphaned" }>,
    ) =>
        orphan.cause !== "day_left" || orphan.pendingWrite
            ? orphaned[orphan.cause]
            : orphan.attempt
              ? ""
              : t("interval_day_left_unsaved");
    // The answer an orphaned editor last got, unless it is the lost one its attempt tells.
    const orphanAnswer =
        editor.status === "orphaned" &&
        !(editor.error instanceof UnconfirmedError)
            ? editor.error
            : undefined;
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
            {message.text && (
                <Notice>
                    <span key={message.count}>{message.text}</span>
                </Notice>
            )}
            {editor.status === "editing" && editor.reloaded && (
                <Notice>{t("reloaded_draft_preserved")}</Notice>
            )}
            {history && reading.error && (
                <Notice error>
                    {t("refresh_failed", { error: reading.error })}
                </Notice>
            )}
            {editor.status === "orphaned" && (
                <div className="production-row-editor">
                    {editor.attempt ? (
                        <UnconfirmedNotice
                            lead={orphanNotice(editor)}
                            repeatable={orphanRepeats && repeatsAttempt(editor)}
                        />
                    ) : (
                        orphanAnswer === undefined && (
                            <Notice error>{orphanNotice(editor)}</Notice>
                        )
                    )}
                    {orphanAnswer !== undefined && (
                        <WriteFailure
                            error={orphanAnswer}
                            lead={
                                editor.attempt
                                    ? undefined
                                    : orphanNotice(editor)
                            }
                        />
                    )}
                    <ControlField
                        label={t("attempted_energy", {
                            interval: datedInterval(editor.key),
                        })}
                    >
                        <input value={editor.energy} readOnly />
                    </ControlField>
                    <div className="button-group">
                        {editor.attempt &&
                            (orphanRepeats ||
                                (editor.pendingWrite &&
                                    editor.error !== undefined)) && (
                                <button
                                    className="button"
                                    type="button"
                                    disabled={saving}
                                    onClick={repeatOrphan}
                                >
                                    {saving
                                        ? t("saving")
                                        : retained(editor.attempt)}
                                </button>
                            )}
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
                </div>
            )}
            {reading.loading ? (
                <p role="status">{t("loading_production_history")}</p>
            ) : !history ? (
                <>
                    {editor.status !== "closed" &&
                        editor.status !== "orphaned" &&
                        editor.attempt && (
                            <UnconfirmedNotice repeatable={false} />
                        )}
                    <ReadFailed resource={reading} />
                </>
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
                                        total: format.rounded(
                                            day.total_mwh,
                                            "MWh",
                                        ),
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
                            time={time}
                            intervalLabel={intervalLabel}
                            canManage={canManage}
                            canAct={(row) => canAct(access, row)}
                            hasConfiguration={Boolean(configuration)}
                            actionsDisabled={!derived.rowActions}
                            editingKey={editing}
                            editButtons={editButtons}
                            unavailable={unavailable}
                            onOpen={(row) => request({ type: "open", row })}
                            renderEditor={(row) =>
                                editor.status !== "closed" && (
                                    <IntervalEditor
                                        row={row}
                                        rows={rows}
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
                                t("file"),
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
                    onReturn={() => onKeepDay(draft.day)}
                    onDiscard={() => {
                        if (request({ type: "discard" }))
                            finish(proposals("discarded"));
                    }}
                    onReload={reading.reload}
                    onSent={(status) => {
                        apply({ type: "sent" });
                        finish(receipt(status));
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
                        onChanged={() => void reloadAfterSave()}
                    />
                </details>
            )}
        </section>
    );
}
