"use client";
import { useLocale, useTranslations } from "next-intl";
import type { Calibration } from "@/lib/api";
import { calibrationExport } from "@/lib/exports";
import { useFormat } from "@/lib/format/use-format";
import { ConfirmAction } from "@/ui/action";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { ScrollRegion } from "@/ui/scroll-region";
import { JsonDetails, Notice, Stat, Tag } from "@/ui/ui";
import { StatusTag, useCodeLabel, useLabel } from "@/ui/vocabulary";
import {
    provisional,
    type Action,
    type AutomationStatus,
    type RequestState,
} from "./model";

const REQUEST_REFUSALS = {
    configuration: "await_configuration",
    window: "window_outside",
    incomplete_days: "days_incomplete",
    open_run: "run_open",
} as const satisfies Record<
    Exclude<RequestState, "available" | "unknown">,
    string
>;

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

// What an activation does is stated before it is confirmed: plants/activation.py turns automation on.
function ActivationEffect({
    run,
    automation,
}: {
    run: Calibration;
    automation: AutomationStatus | undefined;
}) {
    const t = useTranslations("Calibrations");
    const format = useFormat();
    return (
        <>
            <p>
                {t("activate_effect", {
                    factor: format.number(run.factor, 4),
                    start: run.window_start,
                })}
            </p>
            <p>
                {automation?.enabled
                    ? t("activation_keeps_automation")
                    : t("activation_turns_automation_on")}
            </p>
        </>
    );
}

export function CalibrationRunDetail({
    run,
    action,
    automation,
    canManage,
    request,
    exportPending,
    onAction,
    onActivate,
    onRequest,
}: {
    run: Calibration;
    action: Action;
    automation: AutomationStatus | undefined;
    canManage: boolean;
    request: RequestState;
    exportPending: boolean;
    onAction: (action: Action) => void;
    onActivate: (run: Calibration) => void;
    onRequest: (run: Calibration) => void;
}) {
    const t = useTranslations("Calibrations");
    const format = useFormat();
    const label = useLabel();
    const plant = useExportPlant();
    const assessment = (value: Calibration["assessment"]) =>
        value === "" ? "" : label("CalibrationAssessment", value);
    const blocker = run.activation_blocker;
    const activatable =
        canManage && run.selection_state === "candidate" && blocker === null;
    const confirming =
        (action.status === "confirming" && action.kind === "activate") ||
        (action.status === "pending" && action.kind === "activate");
    return (
        <div className="calibration-detail">
            <div className="section-heading">
                <h3>{t("calibration_id", { id: run.id })}</h3>
                <div className="tags">
                    <StatusTag group="CalibrationStatus" value={run.status} />
                    <StatusTag
                        group="SelectionState"
                        value={run.selection_state}
                    />
                    {provisional(run) && (
                        <Tag tone="warning">{t("provisional_production")}</Tag>
                    )}
                </div>
            </div>
            <p>
                {t("assessment_factor", {
                    assessment: assessment(run.assessment),
                    factor:
                        run.factor === null
                            ? t("unavailable")
                            : format.number(run.factor, 4),
                })}
            </p>
            {blocker !== null && run.selection_state !== "active" && (
                <Notice>
                    <p>
                        {t("blocked", {
                            blocker: label("ActivationBlocker", blocker),
                        })}{" "}
                        {t(`blockers.${blocker}`)}
                    </p>
                    {canManage &&
                        (request === "available" ? (
                            <button
                                className="button secondary"
                                type="button"
                                disabled={action.status === "pending"}
                                onClick={() => onRequest(run)}
                            >
                                {t("request_new", {
                                    start: run.window_start,
                                    end: run.window_end,
                                })}
                            </button>
                        ) : (
                            request !== "unknown" && (
                                <p className="fine">
                                    {t(REQUEST_REFUSALS[request])}
                                </p>
                            )
                        ))}
                </Notice>
            )}
            {activatable &&
                (confirming ? (
                    <ConfirmAction
                        label={t("activate_title", { id: run.id })}
                        cancel={t("cancel")}
                        confirm={t("confirm_activation")}
                        confirming={t("working")}
                        pending={action.status === "pending"}
                        onCancel={() => onAction({ status: "idle" })}
                        onConfirm={() => onActivate(run)}
                    >
                        <strong>{t("activate_title", { id: run.id })}</strong>
                        <ActivationEffect run={run} automation={automation} />
                    </ConfirmAction>
                ) : (
                    <button
                        className="button"
                        type="button"
                        disabled={action.status === "pending"}
                        onClick={() =>
                            onAction({
                                status: "confirming",
                                kind: "activate",
                                run,
                            })
                        }
                    >
                        {t("review_activation")}
                    </button>
                ))}
            <CalibrationAssessment run={run} />
            <CsvDownload
                label={t("download_calibration_details_csv")}
                pending={exportPending}
                build={() => calibrationExport(plant, run)}
            />
            <JsonDetails
                data={run.result}
                label={t("evaluation_metrics_exclusions")}
            />
            <JsonDetails data={run} label={t("run_provenance")} />
        </div>
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
