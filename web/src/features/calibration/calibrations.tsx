"use client";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { Calibration, PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiList, useRefresh } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import { CalibrationAutomationPanel } from "./calibration-automation";
import { CsvDownload } from "@/ui/csv-download";
import { ScrollRegion } from "@/ui/scroll-region";
import { useErrorMessage } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty, JsonDetails, Notice, Stat, Tag } from "@/ui/ui";
import { StatusTag, useCodeLabel, useLabel } from "@/ui/vocabulary";

function useValueLabel() {
    const t = useTranslations("Calibrations");
    const locale = useLocale();
    const code = useCodeLabel("Calibrations.values");
    return (value: string) =>
        value === ""
            ? ""
            : code(
                  value,
                  t("unknown_value", {
                      value:
                          locale === "en" ? value.replaceAll("_", " ") : value,
                  }),
              );
}

type DetailState =
    | { status: "idle" | "loading" }
    | { status: "loaded"; data: Calibration }
    | { status: "error"; error: string };
type ActionState =
    | { status: "idle" | "pending" | "success" }
    | { status: "error"; error: string };

export function Calibrations({
    scope,
    currentConfigurationId,
}: {
    scope: PlantScope;
    currentConfigurationId: number | null;
}) {
    const t = useTranslations("Calibrations");
    const canManage = useCanManageWorkspace();
    const format = useFormat();
    const label = useLabel();
    const errorMessage = useErrorMessage();
    const assessment = (value: Calibration["assessment"]) =>
        value === "" ? "" : label("CalibrationAssessment", value);

    const refresh = useRefresh();
    const changed = () => refresh.workspace(scope.tenant_id);
    const [automationPending, setAutomationPending] = useState(false),
        [detail, setDetail] = useState<DetailState>({ status: "idle" }),
        [actionState, setActionState] = useState<ActionState>({
            status: "idle",
        });
    const selected = detail.status === "loaded" ? detail.data : undefined;
    const detailSequence = useRef(0);
    useEffect(() => {
        const sequence = detailSequence;
        return () => {
            sequence.current++;
        };
    }, [scope.plant_id]);
    const resource = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/",
        { path: scope },
        { poll: cadence.steady },
    );
    async function action(run: () => Promise<unknown>) {
        if (!canManage) return;
        setActionState({ status: "pending" });
        try {
            await run();
            setActionState({ status: "success" });
            await changed();
        } catch (error) {
            setActionState({
                status: "error",
                error: errorMessage(error),
            });
        }
    }
    async function loadDetails(runId: number) {
        const sequence = ++detailSequence.current;
        setDetail({ status: "loading" });
        try {
            const data = await api.get(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/{calibration_id}/",
                { path: { ...scope, calibration_id: runId } },
            );
            if (sequence === detailSequence.current)
                setDetail({ status: "loaded", data });
        } catch (error) {
            if (sequence === detailSequence.current)
                setDetail({ status: "error", error: errorMessage(error) });
        }
    }
    return (
        <>
            <CalibrationAutomationPanel
                key={scope.plant_id}
                scope={scope}
                disabled={actionState.status === "pending"}
                onPendingChange={setAutomationPending}
                onChanged={changed}
            />
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>{t("calibration_history")}</h2>
                        <p className="fine">{t("selection_effect")}</p>
                    </div>
                    <div className="button-group">
                        {resource.data && (
                            <CsvDownload
                                label={t("download_calibration_csv")}
                                filename="calibrations"
                                records={resource.data.results}
                            />
                        )}
                        <button
                            className="button secondary"
                            onClick={() => {
                                if (actionState.status !== "pending")
                                    setActionState({ status: "idle" });
                                void changed();
                            }}
                        >
                            {t("refresh")}
                        </button>
                        {canManage && (
                            <button
                                className="button secondary"
                                disabled={
                                    actionState.status === "pending" ||
                                    automationPending
                                }
                                onClick={() =>
                                    void action(() =>
                                        api.post(
                                            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibration/reset/",
                                            {
                                                path: scope,
                                            },
                                        ),
                                    )
                                }
                            >
                                {t("reset_to_baseline")}
                            </button>
                        )}
                    </div>
                </div>
                {actionState.status === "error" && (
                    <Notice error>{actionState.error}</Notice>
                )}
                {actionState.status === "success" && (
                    <Notice>
                        {t(
                            "calibration_selection_updated_refresh_the_forecast_to",
                        )}
                    </Notice>
                )}
                <Resource
                    resource={resource}
                    loading={t("loading_calibration_runs")}
                >
                    {(data) =>
                        data.results.length ? (
                            <ScrollRegion label={t("calibration_runs")}>
                                <table>
                                    <caption className="sr-only">
                                        {t("calibration_runs")}
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>{t("run")}</th>
                                            <th>{t("window")}</th>
                                            <th>{t("status")}</th>
                                            <th>{t("assessment")}</th>
                                            <th>{t("factor")}</th>
                                            <th>{t("actions")}</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {data.results.map((run) => (
                                            <tr key={run.id}>
                                                <td>#{run.id}</td>
                                                <td>
                                                    {run.window_start} →{" "}
                                                    {run.window_end}
                                                </td>
                                                <td>
                                                    <StatusTag
                                                        group="CalibrationStatus"
                                                        value={run.status}
                                                    />
                                                    {run.configuration_id !==
                                                        currentConfigurationId && (
                                                        <Tag>
                                                            {t(
                                                                "previous_configuration",
                                                            )}
                                                        </Tag>
                                                    )}
                                                </td>
                                                <td>
                                                    {assessment(run.assessment)}{" "}
                                                    {(
                                                        run.result as Record<
                                                            string,
                                                            unknown
                                                        > | null
                                                    )
                                                        ?.provisional_production ===
                                                        true && (
                                                        <Tag tone="warning">
                                                            {t(
                                                                "provisional_production",
                                                            )}
                                                        </Tag>
                                                    )}
                                                </td>
                                                <td>
                                                    {format.number(
                                                        run.factor,
                                                        4,
                                                    )}
                                                </td>
                                                <td>
                                                    <button
                                                        className="text-button"
                                                        onClick={() =>
                                                            void loadDetails(
                                                                run.id,
                                                            )
                                                        }
                                                    >
                                                        {t("view_details")}
                                                    </button>
                                                    {run.status === "ready" &&
                                                        run.configuration_id !==
                                                            currentConfigurationId && (
                                                            <span>
                                                                {t(
                                                                    "only_the_current_configuration_can_be_activated",
                                                                )}
                                                            </span>
                                                        )}
                                                    {canManage &&
                                                        run.status ===
                                                            "ready" &&
                                                        run.configuration_id ===
                                                            currentConfigurationId && (
                                                            <button
                                                                className="text-button"
                                                                disabled={
                                                                    actionState.status ===
                                                                        "pending" ||
                                                                    automationPending
                                                                }
                                                                onClick={() =>
                                                                    void action(
                                                                        () =>
                                                                            api.post(
                                                                                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibrations/{calibration_id}/activate/",
                                                                                {
                                                                                    path: {
                                                                                        ...scope,
                                                                                        calibration_id:
                                                                                            run.id,
                                                                                    },
                                                                                },
                                                                            ),
                                                                    )
                                                                }
                                                            >
                                                                {t("activate")}
                                                            </button>
                                                        )}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </ScrollRegion>
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
                    <div className="calibration-detail">
                        <div className="section-heading">
                            <h3>{t("calibration_id", { id: selected.id })}</h3>
                            <div className="tags">
                                <StatusTag
                                    group="CalibrationStatus"
                                    value={selected.status}
                                />
                                {(
                                    selected.result as Record<
                                        string,
                                        unknown
                                    > | null
                                )?.provisional_production === true && (
                                    <Tag tone="warning">
                                        {t("provisional_production")}
                                    </Tag>
                                )}
                            </div>
                        </div>
                        <p>
                            {t("assessment_factor", {
                                assessment: assessment(selected.assessment),
                                factor:
                                    selected.factor === null
                                        ? t("unavailable")
                                        : format.number(selected.factor, 4),
                            })}
                        </p>
                        <CalibrationAssessment run={selected} />
                        <CsvDownload
                            label={t("download_calibration_details_csv")}
                            filename={`calibration-${selected.id}`}
                            records={[selected]}
                        />
                        <JsonDetails
                            data={selected.result}
                            label={t("evaluation_metrics_exclusions")}
                        />
                        <JsonDetails
                            data={selected}
                            label={t("run_provenance")}
                        />
                    </div>
                )}
            </section>
        </>
    );
}

function CalibrationAssessment({ run }: { run: Calibration }) {
    const t = useTranslations("Calibrations");
    const formatter = useFormat();
    const label = useLabel();
    const valueLabel = useValueLabel();
    const statusMessage = useCodeLabel("Calibrations.status_messages");
    const reasonMessage = useCodeLabel("Calibrations.reasons");

    const asRecord = (value: unknown): Record<string, unknown> =>
        value && typeof value === "object" && !Array.isArray(value)
            ? (value as Record<string, unknown>)
            : {};
    const result = asRecord(run.result),
        baseline = asRecord(result.baseline),
        candidate = asRecord(result.candidate),
        incumbent = asRecord(result.incumbent),
        support = asRecord(result.training_support),
        exclusions = asRecord(result.exclusions),
        daylightMetrics = result.metric_scope === "daylight_intervals";
    const format = (value: unknown) => formatter.number(value, 4);
    const reasons = Array.isArray(result.reasons)
        ? result.reasons.filter(
              (reason): reason is string => typeof reason === "string",
          )
        : [];
    return (
        <>
            <Notice>
                {statusMessage(
                    run.status,
                    t("calibration_status", {
                        status: label("CalibrationStatus", run.status),
                    }),
                )}
                {run.assessment === "provisional"
                    ? t("_this_is_a_provisional_assessment_based")
                    : ""}
            </Notice>
            {reasons.length > 0 && (
                <ul>
                    {reasons.map((reason) => (
                        <li key={reason}>
                            {reasonMessage(reason, valueLabel(reason))}
                        </li>
                    ))}
                </ul>
            )}
            <div className="stats">
                <Stat label={t("valid_days")} value={result.valid_days} />
                <Stat label={t("training_days")} value={result.training_days} />
                <Stat label={t("heldout_days")} value={result.holdout_days} />
                {Object.keys(support).length > 0 && (
                    <Stat
                        label={t("informative_training_days")}
                        value={t("support_required", {
                            days: format(support.informative_days),
                            minimum: format(support.minimum_days),
                        })}
                    />
                )}
            </div>
            {daylightMetrics && (
                <p className="fine">
                    {t(
                        "metrics_use_whole_meter_intervals_intersecting_daylight",
                    )}
                </p>
            )}
            {Object.keys(candidate).length > 0 && (
                <ScrollRegion label={t("heldout_accuracy_comparison")}>
                    <table>
                        <caption>
                            {t(
                                "heldout_accuracy_comparison_lower_error_is_better",
                            )}
                        </caption>
                        <thead>
                            <tr>
                                <th>{t("metric")}</th>
                                <th>{t("baseline")}</th>
                                <th>{t("candidate")}</th>
                                {Object.keys(incumbent).length > 0 && (
                                    <th>
                                        {t("training_selection", {
                                            factor: format(
                                                result.incumbent_factor,
                                            ),
                                        })}
                                    </th>
                                )}
                            </tr>
                        </thead>
                        <tbody>
                            {(
                                [
                                    [
                                        "interval_mae_mw",
                                        t("interval_mean_absolute_error_mw"),
                                    ],
                                    ["bias_mw", t("interval_bias_mw")],
                                    [
                                        "daily_mae_mwh",
                                        daylightMetrics
                                            ? t("daylight_energy_mae_mwh")
                                            : t(
                                                  "daily_mean_absolute_error_mwh",
                                              ),
                                    ],
                                    [
                                        "daily_bias_mwh",
                                        daylightMetrics
                                            ? t("daylight_energy_bias_mwh")
                                            : t("daily_bias_mwh"),
                                    ],
                                ] as const
                            ).map(([key, label]) => (
                                <tr key={key}>
                                    <th>{label}</th>
                                    <td>{format(baseline[key])}</td>
                                    <td>{format(candidate[key])}</td>
                                    {Object.keys(incumbent).length > 0 && (
                                        <td>{format(incumbent[key])}</td>
                                    )}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </ScrollRegion>
            )}
            {Object.entries(exclusions).some(
                ([, value]) => typeof value === "number" && value > 0,
            ) && (
                <div className="provenance">
                    <strong>{t("excluded_observations")}</strong>
                    <ul>
                        {Object.entries(exclusions)
                            .filter(
                                ([, value]) =>
                                    typeof value === "number" && value > 0,
                            )
                            .map(([key, value]) => (
                                <li key={key}>
                                    {valueLabel(key)}: {formatter.number(value)}
                                </li>
                            ))}
                    </ul>
                </div>
            )}
        </>
    );
}
