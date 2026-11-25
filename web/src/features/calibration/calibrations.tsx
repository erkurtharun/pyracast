"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { Calibration, PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiList, useApiQuery, useRefresh } from "@/lib/api/query";
import { calibrationsExport } from "@/lib/exports";
import { plantDay } from "@/lib/format/time";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { useErrorMessage } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty, Notice } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { CalibrationAutomationPanel } from "./calibration-automation";
import { CalibrationRunDetail } from "./calibration-run-detail";
import { CalibrationRunList } from "./calibration-runs";
import { CalibrationSelection } from "./calibration-selection";
import { calibrationInUse, requestState, type Action, type Run } from "./model";

type DetailState =
    | { status: "idle" | "loading" }
    | { status: "loaded"; data: Calibration }
    | { status: "error"; error: string };

export function Calibrations({ scope }: { scope: PlantScope }) {
    const t = useTranslations("Calibrations");
    const canManage = useCanManageWorkspace();
    const plant = useExportPlant();
    const errorMessage = useErrorMessage();
    const refresh = useRefresh();
    const changed = () => refresh.workspace(scope.tenant_id);
    const [automationPending, setAutomationPending] = useState(false);
    const [detail, setDetail] = useState<DetailState>({ status: "idle" });
    const [action, setAction] = useState<Action>({ status: "idle" });
    // Actions whose reload of the runs is still running; no export is built from runs being reloaded.
    const [performing, setPerforming] = useState(0);
    const detailSequence = useRef(0);
    const opened = useRef<number | null>(null);
    useEffect(() => {
        const sequence = detailSequence;
        return () => {
            sequence.current++;
        };
    }, [scope.plant_id]);
    const runs = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/",
        { path: scope },
        { poll: cadence.steady },
    );
    const automation = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibration/automation/",
        { path: scope },
        { poll: cadence.steady },
    );
    async function loadDetails(runId: number, review = false) {
        const sequence = ++detailSequence.current;
        opened.current = runId;
        setDetail({ status: "loading" });
        try {
            const data = await api.get(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/{calibration_id}/",
                { path: { ...scope, calibration_id: runId } },
            );
            if (sequence !== detailSequence.current) return;
            setDetail({ status: "loaded", data });
            if (
                review &&
                data.selection_state === "candidate" &&
                data.activation_blocker === null
            )
                setAction({
                    status: "confirming",
                    kind: "activate",
                    run: data,
                });
        } catch (error) {
            if (sequence === detailSequence.current)
                setDetail({ status: "error", error: errorMessage(error) });
        }
    }
    async function perform(
        kind: "reset" | "activate" | "request",
        request: () => Promise<unknown>,
    ) {
        if (!canManage) return;
        setAction({ status: "pending", kind });
        setPerforming((count) => count + 1);
        try {
            await request();
            setAction({ status: kind === "request" ? "requested" : "done" });
            await changed();
            if (opened.current !== null) await loadDetails(opened.current);
        } catch (error) {
            setAction({ status: "failed", error: errorMessage(error) });
        } finally {
            setPerforming((count) => count - 1);
        }
    }
    const pending = action.status === "pending" || automationPending;
    // A request needs the current, approved configuration.
    const configurationId = automation.data?.configuration_current
        ? automation.data.configuration_id
        : null;
    const selected = detail.status === "loaded" ? detail.data : undefined;
    const inUse = automation.error ? null : calibrationInUse(automation.data);
    return (
        <>
            <CalibrationAutomationPanel
                key={scope.plant_id}
                scope={scope}
                status={automation}
                disabled={action.status === "pending"}
                onPendingChange={setAutomationPending}
                onChanged={changed}
            />
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>{t("calibration_history")}</h2>
                    </div>
                    <div className="button-group">
                        {runs.data && (
                            <CsvDownload
                                pending={performing > 0}
                                build={() =>
                                    calibrationsExport(
                                        plant,
                                        runs.data?.results ?? [],
                                        t("file"),
                                    )
                                }
                            />
                        )}
                        <button
                            className="button secondary"
                            onClick={() => {
                                if (action.status !== "pending")
                                    setAction({ status: "idle" });
                                void changed();
                            }}
                        >
                            {t("refresh")}
                        </button>
                    </div>
                </div>
                {automation.data && (
                    <CalibrationSelection
                        status={automation.data}
                        loadedAt={
                            automation.error ? automation.updatedAt : null
                        }
                        action={action}
                        canManage={canManage}
                        onAction={setAction}
                        onReset={() =>
                            void perform("reset", () =>
                                api.post(
                                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibration/reset/",
                                    { path: scope },
                                ),
                            )
                        }
                    />
                )}
                {action.status === "failed" && (
                    <Notice error>{action.error}</Notice>
                )}
                {action.status === "done" && (
                    <Notice>
                        {t(
                            "calibration_selection_updated_refresh_the_forecast_to",
                        )}
                    </Notice>
                )}
                {action.status === "requested" && (
                    <Notice>{t("request_sent")}</Notice>
                )}
                <Resource
                    resource={runs}
                    loading={t("loading_calibration_runs")}
                >
                    {(data) =>
                        data.results.length ? (
                            <CalibrationRunList
                                runs={data.results}
                                inUse={inUse}
                                canManage={canManage}
                                busy={pending}
                                onDetails={(run: Run) =>
                                    void loadDetails(run.id)
                                }
                                onReview={(run: Run) =>
                                    void loadDetails(run.id, true)
                                }
                            />
                        ) : (
                            <Empty title={t("no_calibration_runs")}>
                                {t(
                                    "save_complete_paired_days_in_the_production",
                                )}
                            </Empty>
                        )
                    }
                </Resource>
                {detail.status === "error" && (
                    <Notice error>{detail.error}</Notice>
                )}
                {detail.status === "loading" && (
                    <p role="status">{t("loading_calibration_details")}</p>
                )}
                {selected && (
                    <CalibrationRunDetail
                        run={selected}
                        inUse={selected.id === inUse}
                        action={action}
                        automation={automation.data}
                        canManage={canManage}
                        exportPending={performing > 0}
                        request={requestState(
                            selected,
                            runs.data?.results,
                            automation.data,
                            plantDay(plant.timezone),
                        )}
                        onAction={setAction}
                        onActivate={(run) =>
                            void perform("activate", () =>
                                api.post(
                                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/{calibration_id}/activate/",
                                    {
                                        path: {
                                            ...scope,
                                            calibration_id: run.id,
                                        },
                                    },
                                ),
                            )
                        }
                        onRequest={(run) => {
                            if (configurationId === null) return;
                            void perform("request", () =>
                                api.post(
                                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/",
                                    {
                                        path: scope,
                                        body: {
                                            configuration_id: configurationId,
                                            start_date: run.window_start,
                                            end_date: run.window_end,
                                        },
                                    },
                                ),
                            );
                        }}
                    />
                )}
            </section>
        </>
    );
}
