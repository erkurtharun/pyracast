import type { Calibration } from "@/lib/api";
import type { components } from "@/lib/schema";

export type Run = components["schemas"]["CalibrationSummaryOutput"];
export type AutomationStatus =
    components["schemas"]["CalibrationAutomationOutput"];
export type Blocker = NonNullable<Calibration["activation_blocker"]>;

export type Action =
    | { status: "idle" | "done" }
    | { status: "confirming"; kind: "reset" }
    | { status: "confirming"; kind: "activate"; run: Calibration }
    | { status: "pending"; kind: "reset" | "activate" | "request" }
    | { status: "requested" }
    | { status: "failed"; error: string };

const DAY_MS = 86_400_000;

export type RequestState =
    | "unknown"
    | "available"
    | "configuration"
    | "window"
    | "incomplete_days"
    | "open_run";

// request_calibration's refusals in its order; `today` is the plant's local date, the binding's zone.
export function requestState(
    run: Pick<Run, "window_start" | "window_end">,
    runs: readonly Pick<Run, "status">[] | undefined,
    automation: Pick<AutomationStatus, "configuration_current"> | undefined,
    today: string,
): RequestState {
    if (!runs || !automation) return "unknown";
    if (!automation.configuration_current) return "configuration";
    const days =
        (Date.parse(run.window_end) - Date.parse(run.window_start)) / DAY_MS +
        1;
    if (days < 7 || days > 90) return "window";
    if (run.window_end >= today) return "incomplete_days";
    if (
        runs.some((row) => row.status === "pending" || row.status === "running")
    )
        return "open_run";
    return "available";
}

export function canReset(status: AutomationStatus) {
    return (
        status.configuration_current &&
        (status.enabled ||
            (status.current_calibration?.calibration_id ?? null) !== null)
    );
}

export function provisional(run: Pick<Run, "result">) {
    const report = run.result;
    return (
        !!report &&
        typeof report === "object" &&
        Object.hasOwn(report, "provisional_production") &&
        Reflect.get(report, "provisional_production") === true
    );
}
