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
import { qualityOf } from "@/lib/production";
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
    reconcileEditor,
    saveFailure,
    type Draft,
    type Editor,
    type History,
    type Interval,
} from "./history/model";
import { stageChange, unstageChange } from "./history/proposal";
import { ProposalDraft, type Proposal } from "./history/proposal-draft";
import { ProductionTable } from "./history/production-table";
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
    const [showFullDay, setShowFullDay] = useState(false);
    const [editor, setEditor] = useState<Editor>({ status: "closed" });
    const [proposal, setProposal] = useState<Proposal | null>(null);
    const [message, setMessage] = useState("");
    // Saves whose reload of these rows is still running; no export is built from rows being reloaded.
    const [reloads, setReloads] = useState(0);
    const editButtons = useRef(new Map<string, HTMLButtonElement>());
    const pendingFocus = useRef<string | null>(null);
    const summaryRef = useRef<HTMLDivElement>(null);
    const reading = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/",
        {
            path: scope,
            query: { start_date: date, end_date: date, native_intervals: true },
        },
        { poll: editor.status === "closed" ? cadence.live : false },
    );
    if (reading.data && reading.data !== reconciled) {
        const fresh = reading.data;
        setReconciled(fresh);
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
    const day = history?.days.find((day) => day.date === date);
    const daylight = daylightRows(rows, day?.daylight_windows ?? []);
    const visibleRows = showFullDay || !daylight.length ? rows : daylight;
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
    const canAct = (row: Interval) =>
        canManage ? row.can_edit : row.can_propose;
    const staged = (key: string) =>
        proposal?.day === date
            ? proposal.items.find((item) => item.key === key)
            : undefined;
    const saving =
        editor.status === "saving" ||
        editor.status === "reloading" ||
        (editor.status === "orphaned" && editor.pendingWrite === true);

    async function reloadAfterSave() {
        setReloads((count) => count + 1);
        try {
            await onSaved();
        } finally {
            setReloads((count) => count - 1);
        }
    }
    function selectDate(value: string) {
        if (!value || value > yesterday || saving || proposal) return;
        onDay(value);
        setEditor({ status: "closed" });
        setMessage("");
    }
    function close(key: string) {
        pendingFocus.current = key;
        setEditor({ status: "closed" });
    }
    function open(row: Interval) {
        const item = staged(intervalKey(row));
        setEditor({
            status: "editing",
            key: intervalKey(row),
            energy:
                item?.action === "set"
                    ? item.energy.toString()
                    : (row.effective?.energy_mwh.toString() ?? ""),
            quality:
                item?.action === "set"
                    ? item.quality
                    : (row.observation?.quality ?? "valid"),
            expectedRevision: row.revision_id,
        });
    }
    async function reloadEditor() {
        if (editor.status === "closed" || saving) return;
        const attempt = { ...editor, implausible: false };
        setEditor({ ...attempt, status: "reloading" });
        try {
            const updated = await reading.reload();
            const row = updated.intervals.find(
                (row) => intervalKey(row) === attempt.key,
            );
            if (row && canAct(row)) {
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
                    pendingWrite: false,
                });
            }
        } catch (error) {
            setEditor((current) =>
                current.status === "orphaned" && current.key === attempt.key
                    ? current
                    : {
                          ...attempt,
                          status: "failed",
                          error: errorMessage(error),
                      },
            );
        }
    }
    function attemptFor(row: Interval): Draft {
        return editor.status !== "closed" && editor.key === intervalKey(row)
            ? editor
            : {
                  key: intervalKey(row),
                  energy: row.effective?.energy_mwh.toString() ?? "",
                  quality: qualityOf(row.observation?.quality),
                  expectedRevision: row.revision_id,
              };
    }
    function stage(row: Interval, action: "set" | "revert") {
        if (!configuration || !row.can_propose) return;
        if (action === "revert" && !row.can_propose_revert) return;
        const attempt = attemptFor(row);
        const energy = energyValue(attempt.energy);
        if (action === "set" && energy === null) {
            setEditor({
                ...attempt,
                status: "failed",
                error: t("invalid_energy"),
            });
            return;
        }
        const bounds = {
            key: attempt.key,
            starts_at: row.starts_at,
            ends_at: row.ends_at,
            expectedRevision: attempt.expectedRevision,
        };
        setProposal((current) => ({
            day: date,
            items: stageChange(
                current?.day === date ? current.items : [],
                action === "set" && energy !== null
                    ? { ...bounds, action, energy, quality: attempt.quality }
                    : { ...bounds, action: "revert" },
            ),
        }));
        setMessage(
            proposals("staged", {
                interval: intervalLabel(row),
            }),
        );
        close(attempt.key);
    }
    function unstage(key: string) {
        const items = unstageChange(proposal?.items ?? [], key);
        setProposal(items.length && proposal ? { ...proposal, items } : null);
        if (!items.length) summaryRef.current?.focus();
    }
    function finishProposal(text: string) {
        setProposal(null);
        setMessage(text);
        summaryRef.current?.focus();
    }
    async function save(
        row: Interval,
        action: "set" | "revert",
        confirmImplausible = false,
    ) {
        if (
            !configuration ||
            saving ||
            !history ||
            !row.can_edit ||
            (action === "revert" && !row.can_revert)
        )
            return;
        const attempt = attemptFor(row);
        const energy = energyValue(attempt.energy);
        if (action === "set" && energy === null) {
            setEditor({
                ...attempt,
                status: "failed",
                error: t("invalid_energy"),
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
            setEditor({ status: "closed" });
            setMessage(action === "revert" ? t("reverted") : t("saved"));
            await reloadAfterSave();
        } catch (error) {
            setEditor((current) =>
                current.status === "orphaned" && current.key === attempt.key
                    ? { ...current, pendingWrite: false }
                    : {
                          ...attempt,
                          status: "failed",
                          ...saveFailure(error),
                          error: errorMessage(error),
                      },
            );
        }
    }
    const unavailable = (row: Interval | undefined) => {
        const code = canManage
            ? row?.edit_unavailable_reason
            : row?.proposal_unavailable_reason;
        return code ? reason(code, t("unavailable")) : t("unavailable");
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
                <ProductionDayNav
                    date={date}
                    stepFrom={stepFrom}
                    yesterday={yesterday}
                    disabled={saving || proposal !== null}
                    onSelect={selectDate}
                />
                <CheckboxField
                    label={t("show_full_day")}
                    checked={showFullDay}
                    disabled={editor.status !== "closed"}
                    onChange={setShowFullDay}
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
                    <Notice error>{editor.error}</Notice>
                    <ControlField
                        label={t("attempted_energy", {
                            interval: editor.key.split("/").map(time).join("–"),
                        })}
                    >
                        <input value={editor.energy} readOnly />
                    </ControlField>
                    <button
                        className="button secondary"
                        type="button"
                        disabled={saving}
                        onClick={() => close(editor.key)}
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
                            canAct={canAct}
                            hasConfiguration={Boolean(configuration)}
                            editorOpen={editor.status !== "closed"}
                            editingKey={
                                editor.status !== "closed" &&
                                editor.status !== "orphaned"
                                    ? editor.key
                                    : undefined
                            }
                            editButtons={editButtons}
                            unavailable={unavailable}
                            onOpen={open}
                            renderEditor={(row) =>
                                editor.status !== "closed" && (
                                    <IntervalEditor
                                        row={row}
                                        editor={editor}
                                        interval={intervalLabel(row)}
                                        canManage={canManage}
                                        staged={Boolean(
                                            staged(intervalKey(row)),
                                        )}
                                        saving={saving}
                                        onChange={(change) =>
                                            setEditor({
                                                ...editor,
                                                status: "editing",
                                                ...change,
                                                error: undefined,
                                                implausible: false,
                                            })
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
                                        onCancel={() => close(intervalKey(row))}
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
            {!canManage && configuration && proposal && (
                <ProposalDraft
                    scope={scope}
                    configuration={configuration}
                    proposal={proposal}
                    rows={
                        proposal.day === date ? history?.intervals : undefined
                    }
                    intervalLabel={intervalLabel}
                    editing={editor.status !== "closed"}
                    onEdit={(key) => {
                        const row = rows.find(
                            (row) => intervalKey(row) === key,
                        );
                        if (row) open(row);
                    }}
                    onRemove={unstage}
                    onDiscard={() => finishProposal(proposals("discarded"))}
                    onReload={reading.reload}
                    onSent={() => {
                        finishProposal(proposals("submitted"));
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
