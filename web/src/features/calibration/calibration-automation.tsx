"use client";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import { CheckboxField, useErrorMessage } from "@/ui/form";
import { Notice, RefreshFailed } from "@/ui/ui";
import { useCodeLabel } from "@/ui/vocabulary";

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
    const format = useFormat();
    const errorMessage = useErrorMessage();
    const reason = useCodeLabel("CalibrationAutomation.reasons");

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
            if (live.current) setError(errorMessage(reason));
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
                    <CheckboxField
                        label={t("automatic_calibration_enabled")}
                        checked={data.enabled}
                        disabled={!canManage || disabled || pending}
                        onChange={toggle}
                    />
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
                            ? format.deviceTime(data.next_check_at)
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
                                        date: format.deviceTime(
                                            data.last_attempt.created_at,
                                        ),
                                    },
                                )}{" "}
                                {reason(
                                    data.last_attempt.reason,
                                    t("unknown_reason", {
                                        reason:
                                            locale === "en"
                                                ? data.last_attempt.reason.replaceAll(
                                                      "_",
                                                      " ",
                                                  )
                                                : data.last_attempt.reason,
                                    }),
                                )}
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
