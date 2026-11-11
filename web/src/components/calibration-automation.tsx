import { useEffect, useRef, useState } from "react";
import { request, type CalibrationAutomation } from "@/lib/api";
import { Notice, useResource } from "./ui";

const reasons: Record<string, string> = {
    accepted: "The candidate passed evaluation and was activated.",
    insufficient_current_source_validation:
        "Waiting for 30 matched days from the current live reference source, including seven held-out days.",
    authorization_revoked: "Authorization is no longer available.",
    configuration_changed: "The plant configuration changed.",
    insufficient_automatic_evaluation:
        "At least 30 matched days and seven held-out days are required.",
    awaiting_new_holdout_days: "Waiting for seven new held-out days.",
    stale_automatic_evaluation:
        "The held-out evidence is too old or too spread out for automatic activation.",
    invalid_incumbent_comparison:
        "The comparison with the current calibration was incomplete.",
    incumbent_improvement_below_margin:
        "The candidate did not improve enough over the current calibration.",
    candidate_rejected: "The candidate did not pass evaluation.",
    candidate_insufficient: "The candidate did not have enough eligible data.",
    candidate_failed: "The candidate could not be evaluated.",
    candidate_superseded: "The candidate was superseded by newer data.",
};

type PanelProps = {
    base: string;
    token: string;
    refresh: number;
    disabled: boolean;
    onPendingChange: (pending: boolean) => void;
    onChanged: () => void;
};

export function CalibrationAutomationPanel(props: PanelProps) {
    const [revision, setRevision] = useState(0);
    return (
        <AutomationStatus
            key={`${props.base}-${revision}`}
            {...props}
            onSaved={() => {
                setRevision((value) => value + 1);
                props.onChanged();
            }}
        />
    );
}

function AutomationStatus({
    base,
    token,
    refresh,
    disabled,
    onPendingChange,
    onSaved,
}: PanelProps & { onSaved: () => void }) {
    const [pending, setPending] = useState(false);
    const [error, setError] = useState<string>();
    const live = useRef(true);
    useEffect(() => {
        live.current = true;
        return () => {
            live.current = false;
        };
    }, []);
    const resource = useResource<CalibrationAutomation>(
        `${base}calibration/automation/`,
        refresh,
        30_000,
    );
    async function toggle(enabled: boolean) {
        setPending(true);
        onPendingChange(true);
        setError(undefined);
        try {
            await request<CalibrationAutomation>(
                `${base}calibration/automation/`,
                "PATCH",
                { enabled },
                token,
            );
            if (live.current) onSaved();
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
                        Automatic calibration
                    </h2>
                    <p className="fine">
                        Daily checks prepare and evaluate transfer-power
                        calibration as matched production and reference data
                        become available. Automatic activation requires at least
                        30 matched days from the same current live reference
                        source, including seven held-out days. Imported history
                        alone cannot establish the accuracy of that live source.
                        A rejected candidate leaves the last good calibration in
                        use.
                    </p>
                </div>
                {data && !resource.error && (
                    <button
                        className="button secondary"
                        disabled={disabled || pending}
                        onClick={() => toggle(!data.enabled)}
                    >
                        {data.enabled
                            ? "Disable automatic calibration"
                            : "Enable automatic calibration"}
                    </button>
                )}
            </div>
            {resource.loading && (
                <p role="status">Loading automatic calibration…</p>
            )}
            {resource.error && <Notice error>{resource.error}</Notice>}
            {error && <Notice error>{error}</Notice>}
            {data && !resource.error && (
                <>
                    <p>
                        <strong>Status:</strong>{" "}
                        {data.enabled
                            ? data.authorized
                                ? "Enabled"
                                : "Enabled, authorization unavailable"
                            : "Disabled"}
                    </p>
                    <p>
                        <strong>Next daily check:</strong>{" "}
                        {data.next_check_at
                            ? new Date(data.next_check_at).toLocaleString()
                            : "Not scheduled"}
                    </p>
                    <p>
                        <strong>Last attempt:</strong>{" "}
                        {data.last_attempt ? (
                            <>
                                {data.last_attempt.status === "activated"
                                    ? "Activated"
                                    : "Skipped"}{" "}
                                {data.last_attempt.calibration_id !== null
                                    ? `calibration #${data.last_attempt.calibration_id} `
                                    : ""}
                                on{" "}
                                {new Date(
                                    data.last_attempt.created_at,
                                ).toLocaleString()}
                                .{" "}
                                {reasons[data.last_attempt.reason] ??
                                    data.last_attempt.reason.replaceAll(
                                        "_",
                                        " ",
                                    )}
                            </>
                        ) : (
                            "No attempts yet."
                        )}
                    </p>
                    <p className="fine">
                        Disabling automatic calibration keeps the last good
                        calibration. Resetting to baseline also disables
                        automation.
                    </p>
                </>
            )}
        </section>
    );
}
