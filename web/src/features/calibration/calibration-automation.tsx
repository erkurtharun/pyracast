"use client";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import type { Loaded } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import { CheckboxField, useErrorMessage } from "@/ui/form";
import { Notice, ReadFailed, RefreshFailed } from "@/ui/ui";
import { useCodeLabel, useLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import type { AutomationStatus } from "./model";

type Toggle =
    { status: "idle" | "saving" } | { status: "failed"; error: string };

export function CalibrationAutomationPanel({
    scope,
    status: resource,
    disabled,
    onPendingChange,
    onChanged,
}: {
    scope: PlantScope;
    status: Loaded<AutomationStatus>;
    disabled: boolean;
    onPendingChange: (pending: boolean) => void;
    onChanged: () => Promise<unknown>;
}) {
    const t = useTranslations("CalibrationAutomation");
    const locale = useLocale();
    const format = useFormat();
    const label = useLabel();
    const errorMessage = useErrorMessage();
    const reason = useCodeLabel("CalibrationAutomation.reasons");
    const canManage = useCanManageWorkspace();
    const [toggle, setToggle] = useState<Toggle>({ status: "idle" });
    const live = useRef(true);
    useEffect(() => {
        live.current = true;
        return () => {
            live.current = false;
        };
    }, []);
    async function change(enabled: boolean) {
        if (!canManage) return;
        setToggle({ status: "saving" });
        onPendingChange(true);
        try {
            await api.patch(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/calibration/automation/",
                { path: scope, body: { enabled } },
            );
            await onChanged();
            if (live.current) setToggle({ status: "idle" });
        } catch (error) {
            if (live.current)
                setToggle({ status: "failed", error: errorMessage(error) });
        } finally {
            if (live.current) onPendingChange(false);
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
                        disabled={
                            !canManage || disabled || toggle.status === "saving"
                        }
                        onChange={(enabled) => void change(enabled)}
                    />
                )}
            </div>
            {resource.loading && (
                <p role="status">{t("loading_automatic_calibration")}</p>
            )}
            <RefreshFailed resource={resource} />
            {resource.error && !data && <ReadFailed resource={resource} />}
            {toggle.status === "failed" && (
                <Notice error>{toggle.error}</Notice>
            )}
            {data && (
                <>
                    {data.enabled && !data.authorized && (
                        <Notice>
                            {t(
                                "authorization_unavailable_calibration_is_paused",
                            )}
                        </Notice>
                    )}
                    {data.enabled && !data.configuration_current && (
                        <Notice>{t("paused_configuration")}</Notice>
                    )}
                    {!data.enabled && data.disabled_cause !== null && (
                        <p className="fine">
                            {t("disabled_because", {
                                cause: label(
                                    "AutomationCause",
                                    data.disabled_cause,
                                ),
                            })}
                        </p>
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
