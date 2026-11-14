import { useEffect, useRef, useState } from "react";
import { request, type Calibration } from "@/lib/api";
import { CalibrationAutomationPanel } from "./calibration-automation";
import { CsvDownload } from "./csv-download";
import { ScrollRegion } from "./scroll-region";
import { Empty, JsonDetails, Notice, Stat, Tag, useCollection } from "./ui";

type DetailState =
    | { status: "idle" | "loading" }
    | { status: "loaded"; data: Calibration }
    | { status: "error"; error: string };
type ActionState =
    | { status: "idle" | "pending" | "success" }
    | { status: "error"; error: string };

export function Calibrations({
    base,
    token,
    productionRevision,
    currentConfigurationId,
}: {
    base: string;
    token: string;
    productionRevision: number;
    currentConfigurationId: number | null;
}) {
    const [version, setVersion] = useState(0),
        [automationPending, setAutomationPending] = useState(false),
        [detail, setDetail] = useState<DetailState>({ status: "idle" }),
        [actionState, setActionState] = useState<ActionState>({
            status: "idle",
        });
    const selected = detail.status === "loaded" ? detail.data : undefined;
    const detailSequence = useRef(0);
    useEffect(() => {
        const sequence = detailSequence;
        return () => {
            sequence.current++;
        };
    }, [base]);
    const resource = useCollection<Calibration>(
        `${base}calibrations/`,
        version + productionRevision,
        30_000,
    );
    async function action(url: string) {
        setActionState({ status: "pending" });
        try {
            await request(url, "POST", {}, token);
            setActionState({ status: "success" });
            setVersion((current) => current + 1);
        } catch (error) {
            setActionState({
                status: "error",
                error: (error as Error).message,
            });
        }
    }
    async function loadDetails(runId: number) {
        const sequence = ++detailSequence.current;
        setDetail({ status: "loading" });
        try {
            const data = await request<Calibration>(
                `${base}calibrations/${runId}/`,
            );
            if (sequence === detailSequence.current)
                setDetail({ status: "loaded", data });
        } catch (error) {
            if (sequence === detailSequence.current)
                setDetail({ status: "error", error: (error as Error).message });
        }
    }
    return (
        <>
            <CalibrationAutomationPanel
                key={base}
                base={base}
                token={token}
                refresh={version + productionRevision}
                disabled={actionState.status === "pending"}
                onPendingChange={setAutomationPending}
                onChanged={() => setVersion((current) => current + 1)}
            />
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>Calibration history</h2>
                        <p className="fine">
                            Future forecasts only. Reset also turns off
                            automatic calibration.
                        </p>
                    </div>
                    <div className="button-group">
                        {resource.data &&
                            !resource.loading &&
                            !resource.error && (
                                <CsvDownload
                                    label="Download calibration CSV"
                                    filename="calibrations"
                                    records={resource.data.results}
                                />
                            )}
                        <button
                            className="button secondary"
                            onClick={() => setVersion((current) => current + 1)}
                        >
                            Refresh
                        </button>
                        <button
                            className="button secondary"
                            disabled={
                                actionState.status === "pending" ||
                                automationPending
                            }
                            onClick={() => action(`${base}calibration/reset/`)}
                        >
                            Reset to baseline
                        </button>
                    </div>
                </div>
                {actionState.status === "error" && (
                    <Notice error>{actionState.error}</Notice>
                )}
                {actionState.status === "success" && (
                    <Notice>
                        Calibration selection updated. Refresh the forecast to
                        see the current result.
                    </Notice>
                )}
                {resource.loading ? (
                    <p role="status">Loading calibration runs…</p>
                ) : resource.error ? (
                    <Notice error>{resource.error}</Notice>
                ) : resource.data?.results.length ? (
                    <ScrollRegion label="Calibration runs">
                        <table>
                            <caption className="sr-only">
                                Calibration runs
                            </caption>
                            <thead>
                                <tr>
                                    <th>Run</th>
                                    <th>Window</th>
                                    <th>Status</th>
                                    <th>Assessment</th>
                                    <th>Factor</th>
                                    <th>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {resource.data.results.map((run) => (
                                    <tr key={run.id}>
                                        <td>#{run.id}</td>
                                        <td>
                                            {run.window_start} →{" "}
                                            {run.window_end}
                                        </td>
                                        <td>
                                            <Tag>{run.status}</Tag>
                                            {run.configuration_id !==
                                                currentConfigurationId && (
                                                <Tag>
                                                    Previous configuration
                                                </Tag>
                                            )}
                                        </td>
                                        <td>
                                            {run.assessment}{" "}
                                            {(
                                                run.result as Record<
                                                    string,
                                                    unknown
                                                > | null
                                            )?.provisional_production ===
                                                true && (
                                                <Tag tone="warning">
                                                    Provisional production
                                                </Tag>
                                            )}
                                        </td>
                                        <td>{run.factor ?? "—"}</td>
                                        <td>
                                            <button
                                                className="text-button"
                                                onClick={() =>
                                                    loadDetails(run.id)
                                                }
                                            >
                                                View details
                                            </button>
                                            {run.status === "ready" &&
                                                run.configuration_id !==
                                                    currentConfigurationId && (
                                                    <span>
                                                        Only the current
                                                        configuration can be
                                                        activated.
                                                    </span>
                                                )}
                                            {run.status === "ready" &&
                                                run.configuration_id ===
                                                    currentConfigurationId && (
                                                    <button
                                                        className="text-button"
                                                        disabled={
                                                            actionState.status ===
                                                                "pending" ||
                                                            automationPending
                                                        }
                                                        onClick={() =>
                                                            action(
                                                                `${base}calibrations/${run.id}/activate/`,
                                                            )
                                                        }
                                                    >
                                                        Activate
                                                    </button>
                                                )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </ScrollRegion>
                ) : (
                    <Empty title="No calibration runs">
                        Save complete paired days in the production journal to
                        prepare a candidate automatically.
                    </Empty>
                )}
                {detail.status === "error" && (
                    <Notice error>{detail.error}</Notice>
                )}
                {detail.status === "loading" && (
                    <p role="status">Loading calibration details…</p>
                )}
                {selected && (
                    <div className="calibration-detail">
                        <div className="section-heading">
                            <h3>Calibration #{selected.id}</h3>
                            <div className="tags">
                                <Tag>{selected.status}</Tag>
                                {(
                                    selected.result as Record<
                                        string,
                                        unknown
                                    > | null
                                )?.provisional_production === true && (
                                    <Tag tone="warning">
                                        Provisional production
                                    </Tag>
                                )}
                            </div>
                        </div>
                        <p>
                            Assessment: {selected.assessment}. Factor:{" "}
                            {selected.factor ?? "unavailable"}.
                        </p>
                        <CalibrationAssessment run={selected} />
                        <CsvDownload
                            label="Download calibration details CSV"
                            filename={`calibration-${selected.id}`}
                            records={[selected]}
                        />
                        <JsonDetails
                            data={selected.result}
                            label="Evaluation metrics & exclusions"
                        />
                        <JsonDetails data={selected} label="Run provenance" />
                    </div>
                )}
            </section>
        </>
    );
}

function CalibrationAssessment({ run }: { run: Calibration }) {
    const asRecord = (value: unknown): Record<string, unknown> =>
        value && typeof value === "object" && !Array.isArray(value)
            ? (value as Record<string, unknown>)
            : {};
    const result = asRecord(run.result),
        baseline = asRecord(result.baseline),
        candidate = asRecord(result.candidate),
        incumbent = asRecord(result.incumbent),
        support = asRecord(result.training_support),
        exclusions = asRecord(result.exclusions),
        daylightMetrics = result.metric_scope === "daylight_intervals";
    const format = (value: unknown) =>
        typeof value === "number"
            ? new Intl.NumberFormat("en", { maximumFractionDigits: 4 }).format(
                  value,
              )
            : "—";
    const messages: Record<string, string> = {
        pending: "Waiting for the calibration worker.",
        running: "The worker is fitting and evaluating this calibration.",
        ready: "This fit passed the held-out comparison and is ready for explicit activation.",
        rejected:
            "This fit did not pass the improvement criteria. The current selection remains in use.",
        insufficient:
            "There are not enough eligible complete days to evaluate a fit.",
    };
    const reasonMessages: Record<string, string> = {
        insufficient_complete_days:
            "Not enough complete paired days are available.",
        insufficient_training_support:
            "The training days do not contain enough informative production.",
        baseline_improvement_below_margin:
            "The candidate did not improve enough over the baseline.",
        factor_at_bound: "The fitted factor reached its allowed boundary.",
        incumbent_mae_regression:
            "The candidate increased error compared with the selection at training.",
        baseline_bias_regression:
            "The candidate increased bias compared with the baseline.",
        incumbent_bias_regression:
            "The candidate increased bias compared with the selection at training.",
    };
    const reasons = Array.isArray(result.reasons)
        ? result.reasons.filter(
              (reason): reason is string => typeof reason === "string",
          )
        : [];
    return (
        <>
            <Notice>
                {messages[run.status] ?? `Calibration status: ${run.status}.`}
                {run.assessment === "provisional"
                    ? " This is a provisional assessment based on fewer than 30 valid days."
                    : ""}
            </Notice>
            {reasons.length > 0 && (
                <ul>
                    {reasons.map((reason) => (
                        <li key={reason}>
                            {reasonMessages[reason] ??
                                reason.replaceAll("_", " ")}
                        </li>
                    ))}
                </ul>
            )}
            <div className="stats">
                <Stat label="Valid days" value={result.valid_days} />
                <Stat label="Training days" value={result.training_days} />
                <Stat label="Held-out days" value={result.holdout_days} />
                {Object.keys(support).length > 0 && (
                    <Stat
                        label="Informative training days"
                        value={`${format(support.informative_days)} / ${format(support.minimum_days)} required`}
                    />
                )}
            </div>
            {daylightMetrics && (
                <p className="fine">
                    Metrics use whole meter intervals intersecting daylight, not
                    24-hour totals.
                </p>
            )}
            {Object.keys(candidate).length > 0 && (
                <ScrollRegion label="Held-out accuracy comparison">
                    <table>
                        <caption>
                            Held-out accuracy comparison — lower error is better
                        </caption>
                        <thead>
                            <tr>
                                <th>Metric</th>
                                <th>Baseline</th>
                                <th>Candidate</th>
                                {Object.keys(incumbent).length > 0 && (
                                    <th>
                                        Selection at training (factor{" "}
                                        {format(result.incumbent_factor)})
                                    </th>
                                )}
                            </tr>
                        </thead>
                        <tbody>
                            {[
                                [
                                    "interval_mae_mw",
                                    "Interval mean absolute error (MW)",
                                ],
                                ["bias_mw", "Interval bias (MW)"],
                                [
                                    "daily_mae_mwh",
                                    daylightMetrics
                                        ? "Daylight energy MAE (MWh)"
                                        : "Daily mean absolute error (MWh)",
                                ],
                                [
                                    "daily_bias_mwh",
                                    daylightMetrics
                                        ? "Daylight energy bias (MWh)"
                                        : "Daily bias (MWh)",
                                ],
                            ].map(([key, label]) => (
                                <tr key={key}>
                                    <th>{label}</th>
                                    <td>{format(baseline[key])}</td>
                                    <td>{format(candidate[key])}</td>
                                    {Object.keys(incumbent).length > 0 && (
                                        <td>{format(incumbent[key])}</td>
                                    )}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </ScrollRegion>
            )}
            {Object.entries(exclusions).some(
                ([, value]) => typeof value === "number" && value > 0,
            ) && (
                <div className="provenance">
                    <strong>Excluded observations</strong>
                    <ul>
                        {Object.entries(exclusions)
                            .filter(
                                ([, value]) =>
                                    typeof value === "number" && value > 0,
                            )
                            .map(([key, value]) => (
                                <li key={key}>
                                    {key.replaceAll("_", " ")}: {String(value)}
                                </li>
                            ))}
                    </ul>
                </div>
            )}
        </>
    );
}
