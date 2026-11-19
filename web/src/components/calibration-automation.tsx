"use client";
import { useCanManageWorkspace } from "./workspace-access";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery } from "@/lib/api/query";
import { Notice, RefreshFailed } from "./ui";

export function CalibrationAutomationPanel({
    scope,
    disabled,
    onPendingChange,
    onChanged,
}: {
    scope: PlantScope;
    disabled: boolean;
    onPendingChange: (pending: boolean) => void;
    onChanged: () => Promise<unknown>;
}) {
    const t = useTranslations("CalibrationAutomation");
    const locale = useLocale();
    const reasons: Record<string, string> = {
        accepted: t("calibration_activated"),
        insufficient_current_source_validation: t(
            "needs_30_days_from_this_reference_source",
        ),
        derating_suspected: t(
            "production_is_below_the_current_calibration_review",
        ),
        authorization_revoked: t("authorization_is_no_longer_available"),
        configuration_changed: t("the_plant_configuration_changed"),
        insufficient_automatic_evaluation: t(
            "needs_30_matched_days_including_seven_validation",
        ),
        awaiting_new_holdout_days: t("waiting_for_seven_new_heldout_days"),
        stale_automatic_evaluation: t("validation_data_is_too_old_or_too"),
        invalid_incumbent_comparison: t(
            "current_calibration_comparison_is_incomplete",
        ),
        incumbent_improvement_below_margin: t(
            "no_sufficient_improvement_current_calibration_kept",
        ),
        candidate_rejected: t("the_candidate_did_not_pass_evaluation"),
        candidate_insufficient: t("the_candidate_did_not_have_enough_eligible"),
        candidate_failed: t("the_candidate_could_not_be_evaluated"),
        candidate_superseded: t("the_candidate_was_superseded_by_newer_data"),
        preparation_failed: t("the_calibration_could_not_be_prepared"),
        promotion_failed: t("the_candidate_could_not_be_promoted"),
    };

    const [pending, setPending] = useState(false);
    const [error, setError] = useState<string>();
    const live = useRef(true);
    useEffect(() => {
        live.current = true;
        return () => {
            live.current = false;
        };
    }, []);
    const resource = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibration/automation/",
        { path: scope },
        { poll: cadence.steady },
    );
    const canManage = useCanManageWorkspace();
    async function toggle(enabled: boolean) {
        if (!canManage) return;
        setPending(true);
        onPendingChange(true);
        setError(undefined);
        try {
            await api.patch(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibration/automation/",
                {
                    path: scope,
                    body: { enabled },
                },
            );
            await onChanged();
        } catch (reason) {
            if (live.current) setError((reason as Error).message);
        } finally {
            if (live.current) {
                setPending(false);
                onPendingChange(false);
            }
        }
    }
    const data = resource.data;
    return (
        <section
            className="panel"
            aria-labelledby="automatic-calibration-heading"
        >
            <div className="section-heading">
                <div>
                    <h2 id="automatic-calibration-heading">
                        {t("automatic_calibration")}
                    </h2>
                </div>
                {data && (
                    <label className="check-field">
                        <input
                            type="checkbox"
                            aria-label={t("automatic_calibration_enabled")}
                            checked={data.enabled}
                            disabled={!canManage || disabled || pending}
                            onChange={(event) => toggle(event.target.checked)}
                        />{" "}
                        {t("enabled")}
                    </label>
                )}
            </div>
            {resource.loading && (
                <p role="status">{t("loading_automatic_calibration")}</p>
            )}
            <RefreshFailed resource={resource} />
            {resource.error && !data && <Notice error>{resource.error}</Notice>}
            {error && <Notice error>{error}</Notice>}
            {data && (
                <>
                    {data.enabled && !data.authorized && (
                        <Notice>
                            {t(
                                "authorization_unavailable_calibration_is_paused",
                            )}
                        </Notice>
                    )}
                    <p className="fine">
                        <strong>{t("next_check")}</strong>{" "}
                        {data.next_check_at
                            ? new Date(data.next_check_at).toLocaleString(
                                  locale,
                              )
                            : t("not_scheduled")}
                    </p>
                    <p className="fine">
                        <strong>{t("last_result")}</strong>{" "}
                        {data.last_attempt ? (
                            <>
                                {t(
                                    data.last_attempt.calibration_id !== null
                                        ? "attempt_with_calibration"
                                        : "attempt",
                                    {
                                        status:
                                            data.last_attempt.status ===
                                            "activated"
                                                ? t("activated")
                                                : t("skipped"),
                                        calibration:
                                            data.last_attempt.calibration_id ??
                                            0,
                                        date: new Date(
                                            data.last_attempt.created_at,
                                        ).toLocaleString(locale),
                                    },
                                )}{" "}
                                {reasons[data.last_attempt.reason] ??
                                    t("unknown_reason", {
                                        reason:
                                            locale === "en"
                                                ? data.last_attempt.reason.replaceAll(
                                                      "_",
                                                      " ",
                                                  )
                                                : data.last_attempt.reason,
                                    })}
                            </>
                        ) : (
                            t("no_attempts_yet")
                        )}
                    </p>
                    <p className="fine">
                        {t("turning_this_off_keeps_the_current_calibration")}
                    </p>
                </>
            )}
        </section>
    );
}
