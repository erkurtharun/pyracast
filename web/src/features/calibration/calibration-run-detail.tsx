"use client";
import { useTranslations } from "next-intl";
import type { Calibration } from "@/lib/api";
import { calibrationExport } from "@/lib/exports";
import { useFormat } from "@/lib/format/use-format";
import { ConfirmAction } from "@/ui/action";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { Notice, Stat } from "@/ui/ui";
import { useCodeLabel } from "@/ui/vocabulary";
import { CalibrationMarks } from "./calibration-marks";
import {
    firstCalibrationDay,
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
    const factor = format.number(run.factor, 4);
    const start = firstCalibrationDay(run);
    return (
        <>
            <p>
                {start === null
                    ? t("activate_effect_undated", { factor })
                    : t("activate_effect", { factor, start })}
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
    inUse,
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
    inUse: boolean;
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
    const plant = useExportPlant();
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
                <CalibrationMarks run={run} inUse={inUse} />
            </div>
            {blocker !== null && run.selection_state !== "active" && (
                <Notice>
                    <p>
                        {t("blocked", {
                            reason: t(`blockers.${blocker}`),
                        })}
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
                pending={exportPending}
                build={() =>
                    calibrationExport(
                        plant,
                        run,
                        t("detail_file", { id: run.id }),
                        inUse,
                    )
                }
            />
        </div>
    );
}

function CalibrationAssessment({ run }: { run: Calibration }) {
    const t = useTranslations("Calibrations");
    const formatter = useFormat();
    const statusMessage = useCodeLabel("Calibrations.status_messages");
    const reasonMessage = useCodeLabel("Calibrations.reasons");

    const asRecord = (value: unknown): Record<string, unknown> =>
        value && typeof value === "object" && !Array.isArray(value)
            ? (value as Record<string, unknown>)
            : {};
    const result = asRecord(run.result),
        baseline = asRecord(result.baseline),
        candidate = asRecord(result.candidate);
    const reasons = Array.isArray(result.reasons)
        ? result.reasons.filter(
              (reason): reason is string => typeof reason === "string",
          )
        : [];
    const notes = [
        statusMessage(run.status, ""),
        run.assessment === "provisional"
            ? t("_this_is_a_provisional_assessment_based")
            : "",
    ].filter(Boolean);
    return (
        <>
            {notes.length > 0 && <Notice>{notes.join(" ")}</Notice>}
            {reasons.length > 0 && (
                <ul>
                    {reasons.map((reason) => (
                        <li key={reason}>
                            {reasonMessage(reason, t("unknown_value"))}
                        </li>
                    ))}
                </ul>
            )}
            <div className="stats">
                <Stat
                    label={t("factor")}
                    value={formatter.number(run.factor, 4)}
                />
                <Stat
                    label={t("window")}
                    value={`${run.window_start} – ${run.window_end}`}
                />
                <Stat label={t("training_days")} value={result.training_days} />
                <Stat label={t("heldout_days")} value={result.holdout_days} />
                {Object.keys(candidate).length > 0 && (
                    <>
                        <Stat
                            label={t("baseline")}
                            value={formatter.number(baseline.daily_mae_mwh, 2)}
                            unit="MWh"
                        />
                        <Stat
                            label={t("candidate")}
                            value={formatter.number(candidate.daily_mae_mwh, 2)}
                            unit="MWh"
                        />
                    </>
                )}
            </div>
        </>
    );
}
