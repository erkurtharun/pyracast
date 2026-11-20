"use client";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery } from "@/lib/api/query";
import { useProposalKey } from "@/lib/idempotency";
import type { components } from "@/lib/schema";
import { dataIntervalRange, intervalTimeFormatter } from "@/lib/intervals";
import { qualityOf } from "@/lib/production";
import { CsvDownload } from "@/ui/csv-download";
import { CheckboxField, ControlField } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { localDay } from "@/features/forecast/data-source";
import { ProductionDayNav } from "./history/day-nav";
import { IntervalEditor } from "./history/interval-editor";
import {
    intervalKey,
    mostFrequentReason,
    productionCsvRecords,
    reconcileEditor,
    saveFailure,
    type Editor,
    type History,
    type Interval,
} from "./history/model";
import { ProductionTable } from "./history/production-table";
import { ProductionSourceDetails } from "./history/source-details";
import { ProductionImport } from "./production-import";

type ChangeRow = components["schemas"]["ProductionChangeRow"];

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
    const locale = useLocale();
    const canManage = useCanManageWorkspace();
    const proposals = useTranslations("ChangeRequests");
    const proposalKey = useProposalKey();
    const yesterday = localDay(timezone, -1);
    const shown = (day?: string) => (day && day <= yesterday ? day : yesterday);
    const date = shown(requestedDay);
    // A step taken while a day change loads continues from the day being navigated to.
    const stepFrom = shown(baseDay);
    const [showFullDay, setShowFullDay] = useState(false);
    const [editor, setEditor] = useState<Editor>({ status: "closed" });
    const [message, setMessage] = useState("");
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
    const [reconciled, setReconciled] = useState<History>();
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
            ...[
                ...row.provider_originals,
                ...row.previous_configuration_observations,
            ].flatMap((other) => [other.starts_at, other.ends_at]),
        ]),
    );
    const time = (value: string) => timeFormatter.format(new Date(value));
    const intervalLabel = (row: Interval) =>
        `${time(row.starts_at)}–${time(row.ends_at)}`;
    const canAct = (row: Interval) =>
        canManage ? row.can_edit : row.can_propose;
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
    function close(key: string) {
        pendingFocus.current = key;
        setEditor({ status: "closed" });
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
    async function save(
        row: Interval,
        action: "set" | "revert",
        confirmImplausible = false,
    ) {
        if (
            !configuration ||
            !(canManage || row.can_propose) ||
            saving ||
            !history ||
            !canAct(row) ||
            (action === "revert" &&
                !(canManage ? row.can_revert : row.can_propose_revert))
        )
            return;
        const attempt =
            editor.status !== "closed" && editor.key === intervalKey(row)
                ? editor
                : {
                      key: intervalKey(row),
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
                        body: {
                            ...payload,
                            confirm_implausible: confirmImplausible,
                        },
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
                    : { ...attempt, status: "failed", ...saveFailure(error) },
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
                <ProductionDayNav
                    date={date}
                    stepFrom={stepFrom}
                    yesterday={yesterday}
                    disabled={saving}
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
                        <ProductionTable
                            rows={rows}
                            visibleRows={visibleRows}
                            timezone={history.timezone}
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
                            onOpen={(row) =>
                                setEditor({
                                    status: "editing",
                                    key: intervalKey(row),
                                    energy:
                                        row.effective?.energy_mwh.toString() ??
                                        "",
                                    quality:
                                        row.observation?.quality ?? "valid",
                                    expectedRevision: row.revision_id,
                                })
                            }
                            renderEditor={(row) =>
                                editor.status !== "closed" && (
                                    <IntervalEditor
                                        row={row}
                                        editor={editor}
                                        interval={intervalLabel(row)}
                                        canManage={canManage}
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
                                            void save(
                                                row,
                                                action,
                                                confirmImplausible,
                                            )
                                        }
                                        onCancel={() => {
                                            proposalKey.reset();
                                            close(intervalKey(row));
                                        }}
                                        onReload={() => void reloadEditor()}
                                    />
                                )
                            }
                        />
                    )}
                    <CsvDownload
                        label={t("download_csv")}
                        filename={`production-${date}`}
                        records={productionCsvRecords(rows)}
                        metadata={{ timezone: history.timezone, unit: "MWh" }}
                    />
                </>
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
                        onSaved={onSaved}
                    />
                </details>
            )}
        </section>
    );
}
