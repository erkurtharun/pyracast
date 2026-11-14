import { useEffect, useRef, useState } from "react";
import { request, type CalibrationAutomation } from "@/lib/api";
import { Notice, useResource } from "./ui";

const reasons: Record<string, string> = {
    accepted: "Calibration activated.",
    insufficient_current_source_validation:
        "Needs 30 days from this reference source, including seven live validation days.",
    derating_suspected:
        "Production is below the current calibration. Review the comparison.",
    authorization_revoked: "Authorization is no longer available.",
    configuration_changed: "The plant configuration changed.",
    insufficient_automatic_evaluation:
        "Needs 30 matched days, including seven validation days.",
    awaiting_new_holdout_days: "Waiting for seven new held-out days.",
    stale_automatic_evaluation: "Validation data is too old or too spread out.",
    invalid_incumbent_comparison:
        "Current calibration comparison is incomplete.",
    incumbent_improvement_below_margin:
        "No sufficient improvement; current calibration kept.",
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
                </div>
                {data && !resource.error && (
                    <label className="check-field">
                        <input
                            type="checkbox"
                            aria-label="Automatic calibration enabled"
                            checked={data.enabled}
                            disabled={disabled || pending}
                            onChange={(event) => toggle(event.target.checked)}
                        />{" "}
                        Enabled
                    </label>
                )}
            </div>
            {resource.loading && (
                <p role="status">Loading automatic calibration…</p>
            )}
            {resource.error && <Notice error>{resource.error}</Notice>}
            {error && <Notice error>{error}</Notice>}
            {data && !resource.error && (
                <>
                    {data.enabled && !data.authorized && (
                        <Notice>
                            Authorization unavailable. Calibration is paused.
                        </Notice>
                    )}
                    <p className="fine">
                        <strong>Next check:</strong>{" "}
                        {data.next_check_at
                            ? new Date(data.next_check_at).toLocaleString()
                            : "Not scheduled"}
                    </p>
                    <p className="fine">
                        <strong>Last result:</strong>{" "}
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
                        Turning this off keeps the current calibration.
                    </p>
                </>
            )}
        </section>
    );
}
