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

// The request route refuses a window outside 7 to 90 days and any request while a run is open.
export function canRequestAgain(
    run: Pick<Run, "window_start" | "window_end">,
    runs: readonly Pick<Run, "status">[],
) {
    const days =
        (Date.parse(run.window_end) - Date.parse(run.window_start)) / DAY_MS +
        1;
    return (
        days >= 7 &&
        days <= 90 &&
        !runs.some(
            (row) => row.status === "pending" || row.status === "running",
        )
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
