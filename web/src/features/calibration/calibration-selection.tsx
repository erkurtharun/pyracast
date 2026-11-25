"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { ConfirmAction } from "@/ui/action";
import { Notice } from "@/ui/ui";
import { canReset, type Action, type AutomationStatus } from "./model";

export function CalibrationSelection({
    status,
    loadedAt,
    action,
    canManage,
    onAction,
    onReset,
}: {
    status: AutomationStatus;
    loadedAt: number | null;
    action: Action;
    canManage: boolean;
    onAction: (action: Action) => void;
    onReset: () => void;
}) {
    const t = useTranslations("Calibrations");
    const format = useFormat();
    const current = status.current_calibration;
    const chosen =
        current !== null && (!current.effective || loadedAt !== null);
    const resetting =
        (action.status === "confirming" || action.status === "pending") &&
        action.kind === "reset";
    return (
        <section aria-labelledby="calibration-in-force">
            <h3 id="calibration-in-force">
                {chosen ? t("chosen") : t("in_force")}
            </h3>
            {current === null ? (
                <p>{t("in_force_none")}</p>
            ) : (
                <>
                    <p>
                        {current.calibration_id === null
                            ? t("in_force_baseline", {
                                  time: format.deviceTime(current.created_at),
                              })
                            : t("in_force_run", {
                                  id: current.calibration_id,
                                  factor: format.number(current.factor, 4),
                                  time: format.deviceTime(current.created_at),
                              })}{" "}
                        {current.origin === "automatic"
                            ? t("selected_automatically")
                            : t("selected_manually")}
                    </p>
                    {!current.effective && (
                        <Notice>{t("not_effective")}</Notice>
                    )}
                </>
            )}
            {loadedAt !== null && (
                <p className="fine">
                    {t("last_loaded", { time: format.deviceTime(loadedAt) })}
                </p>
            )}
            {canManage &&
                (canReset(status) ||
                    (action.status === "pending" && action.kind === "reset")) &&
                (resetting ? (
                    <ConfirmAction
                        label={t("reset_title")}
                        cancel={t("cancel")}
                        confirm={t("confirm_reset")}
                        confirming={t("working")}
                        pending={action.status === "pending"}
                        onCancel={() => onAction({ status: "idle" })}
                        onConfirm={onReset}
                    >
                        <strong>{t("reset_title")}</strong>
                        <p>{t("reset_effect")}</p>
                    </ConfirmAction>
                ) : (
                    <button
                        className="button secondary"
                        type="button"
                        disabled={action.status === "pending"}
                        onClick={() =>
                            onAction({ status: "confirming", kind: "reset" })
                        }
                    >
                        {t("reset_to_baseline")}
                    </button>
                ))}
        </section>
    );
}
