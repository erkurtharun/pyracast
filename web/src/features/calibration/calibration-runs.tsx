"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { ScrollRegion } from "@/ui/scroll-region";
import { Tag } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import { provisional, type Run } from "./model";

export function CalibrationRunList({
    runs,
    canManage,
    busy,
    onDetails,
    onReview,
}: {
    runs: readonly Run[];
    canManage: boolean;
    busy: boolean;
    onDetails: (run: Run) => void;
    onReview: (run: Run) => void;
}) {
    const t = useTranslations("Calibrations");
    const format = useFormat();
    const label = useLabel();
    return (
        <ScrollRegion label={t("calibration_runs")}>
            <table>
                <caption className="sr-only">{t("calibration_runs")}</caption>
                <thead>
                    <tr>
                        <th>{t("run")}</th>
                        <th>{t("window")}</th>
                        <th>{t("status")}</th>
                        <th>{t("selection_state")}</th>
                        <th>{t("assessment")}</th>
                        <th>{t("factor")}</th>
                        <th>{t("actions")}</th>
                    </tr>
                </thead>
                <tbody>
                    {runs.map((run) => (
                        <tr key={run.id}>
                            <td>#{run.id}</td>
                            <td>
                                {run.window_start} → {run.window_end}
                            </td>
                            <td>
                                <StatusTag
                                    group="CalibrationStatus"
                                    value={run.status}
                                />
                            </td>
                            <td>
                                <StatusTag
                                    group="SelectionState"
                                    value={run.selection_state}
                                />
                            </td>
                            <td>
                                {run.assessment === ""
                                    ? ""
                                    : label(
                                          "CalibrationAssessment",
                                          run.assessment,
                                      )}{" "}
                                {provisional(run) && (
                                    <Tag tone="warning">
                                        {t("provisional_production")}
                                    </Tag>
                                )}
                            </td>
                            <td>{format.number(run.factor, 4)}</td>
                            <td>
                                <button
                                    className="text-button"
                                    onClick={() => onDetails(run)}
                                >
                                    {t("view_details")}
                                </button>
                                {canManage &&
                                    run.selection_state === "candidate" && (
                                        <button
                                            className="text-button"
                                            disabled={busy}
                                            onClick={() => onReview(run)}
                                        >
                                            {t("review_activation")}
                                        </button>
                                    )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </ScrollRegion>
    );
}
