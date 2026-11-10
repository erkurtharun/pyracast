"use client";
import { useRef, useState } from "react";
import {
    request,
    type Plant,
    type Forecast,
    type Calibration,
} from "@/lib/api";
import { parseProduction, type Observation } from "@/lib/production";
import {
    ActionForm,
    Empty,
    JsonDetails,
    Notice,
    Select,
    Stat,
    Tag,
    useResource,
    useCollection,
} from "./ui";
import { ProductionHistory } from "./production-history";
import { MoveToRecycleBin } from "./recycle-bin";
import { ForecastPanel } from "./forecast-panel";
import { ForecastPerformance } from "./forecast-performance";
import { EpiasProduction } from "./epias-production";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";
import { DataSourceControl, localDay, type DataSource } from "./data-source";
export function PlantWorkspace({
    plant,
    base,
    timezone,
    token,
    onBack,
    onDeleted,
}: {
    plant: Plant;
    base: string;
    timezone: string;
    token: string;
    onBack: () => void;
    onDeleted: () => void;
}) {
    const [tab, setTab] = useState("forecast"),
        [date, setDate] = useState(() => {
            const today = new Date().toLocaleDateString("en-CA", {
                timeZone: timezone,
            });
            const tomorrow = new Date(`${today}T00:00:00Z`);
            tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
            return tomorrow.toISOString().slice(0, 10);
        }),
        [resolution, setResolution] = useState("30"),
        [source, setSource] = useState<DataSource>("auto");
    const [productionRevision, setProductionRevision] = useState(0);
    const [currentPlant, setCurrentPlant] = useState(plant);
    const [editing, setEditing] = useState(false);
    const yesterday = localDay(timezone, -1);
    const configuration = currentPlant.current_configuration;
    return (
        <>
            <button className="back-button" onClick={onBack}>
                ← All plants
            </button>
            <div className="section-heading">
                <div>
                    <h2>{currentPlant.name}</h2>
                    <p>
                        Configuration {configuration?.revision ?? "—"} ·{" "}
                        {configuration?.measurement_basis.replaceAll("_", " ")}
                    </p>
                </div>
                <div className="button-group">
                    <Tag>Solar plant</Tag>
                    <button
                        className="button secondary"
                        onClick={() => setEditing(!editing)}
                    >
                        {editing ? "Close configuration" : "Edit configuration"}
                    </button>
                    <MoveToRecycleBin
                        kind="plant"
                        name={currentPlant.name}
                        url={base}
                        token={token}
                        onDeleted={onDeleted}
                    />
                </div>
            </div>
            <div className="stats">
                <Stat
                    label="DC nameplate"
                    value={configuration?.dc_capacity_mw}
                    unit="MW"
                />
                <Stat
                    label="Inverter AC capacity"
                    value={configuration?.inverter_capacity_mw}
                    unit="MW"
                />
                <Stat
                    label="Grid export limit"
                    value={configuration?.export_limit_mw}
                    unit="MW"
                />
            </div>
            <p className="fine">
                DC nameplate is the panel rating; inverter AC capacity is the
                inverter rating; grid export limit is the permitted grid cap.
                The measurement point identifies whether production is measured
                at AC generation or net export. Unknown optional capacities
                remain unspecified.
            </p>
            {editing && configuration && (
                <section className="panel">
                    <h2>Edit plant configuration</h2>
                    <p>
                        Saving creates a new configuration revision and may
                        supersede pending calibration work.
                    </p>
                    <ActionForm
                        submit="Save configuration"
                        onSubmit={async (data) => {
                            const updated = await request<Plant>(
                                base,
                                "PUT",
                                configurationInput(data),
                                token,
                            );
                            setCurrentPlant(updated);
                            setEditing(false);
                        }}
                    >
                        <PlantConfigurationFields
                            name={currentPlant.name}
                            configuration={configuration}
                        />
                    </ActionForm>
                </section>
            )}
            <nav className="tabs" aria-label="Plant sections">
                {[
                    ["forecast", "Generation forecast"],
                    ["production", "Production import"],
                    ["automatic", "Automatic production"],
                    ["calibration", "Calibration"],
                ].map(([key, label]) => (
                    <button
                        key={key}
                        className={tab === key ? "active" : ""}
                        aria-current={tab === key ? "page" : undefined}
                        onClick={() => setTab(key)}
                    >
                        {label}
                    </button>
                ))}
            </nav>
            {tab === "forecast" ? (
                <>
                    <div className="forecast-controls">
                        <label className="field">
                            Forecast date
                            <input
                                type="date"
                                value={date}
                                max={
                                    source === "estimated_actuals"
                                        ? yesterday
                                        : undefined
                                }
                                onChange={(event) =>
                                    setDate(event.target.value)
                                }
                            />
                        </label>
                        <DataSourceControl
                            value={source}
                            onChange={(next) => {
                                setSource(next);
                                if (
                                    next === "estimated_actuals" &&
                                    date > yesterday
                                )
                                    setDate(yesterday);
                            }}
                        />
                        <label className="field">
                            Resolution
                            <select
                                value={resolution}
                                onChange={(event) =>
                                    setResolution(event.target.value)
                                }
                            >
                                <option value="30">30 minutes</option>
                                <option value="60">60 minutes</option>
                            </select>
                        </label>
                        <span className="fine">Local dates in {timezone}</span>
                    </div>
                    {source === "estimated_actuals" && date > yesterday ? (
                        <Notice>
                            Choose a completed local day for estimated actuals.
                        </Notice>
                    ) : (
                        <PlantForecast
                            key={`${date}-${resolution}-${source}-${configuration?.id}`}
                            url={`${base}forecast/?date=${date}&resolution_minutes=${resolution}&data_source=${source}`}
                        />
                    )}
                    <ForecastPerformance
                        key={configuration?.id}
                        base={base}
                        timezone={timezone}
                    />
                </>
            ) : tab === "automatic" ? (
                <EpiasProduction base={base} token={token} />
            ) : !configuration ? (
                <Notice>No configuration is available for this plant.</Notice>
            ) : tab === "production" ? (
                <Production
                    key={configuration.id}
                    base={base}
                    configuration={configuration.id}
                    token={token}
                />
            ) : (
                <>
                    <ProductionHistory
                        key={`history-${configuration.id}`}
                        base={base}
                        configuration={configuration.id}
                        token={token}
                        onSaved={() =>
                            setProductionRevision((current) => current + 1)
                        }
                    />
                    <Calibrations
                        key={`calibration-${configuration.id}`}
                        base={base}
                        token={token}
                        productionRevision={productionRevision}
                    />
                </>
            )}
        </>
    );
}
function PlantForecast({ url }: { url: string }) {
    const resource = useResource<Forecast>(url);
    return (
        <section className="panel">
            {resource.loading ? (
                <p role="status">Loading generation forecast…</p>
            ) : resource.error ? (
                <Notice error>{resource.error}</Notice>
            ) : (
                resource.data && (
                    <ForecastPanel forecast={resource.data} electrical />
                )
            )}
        </section>
    );
}
function Production({
    base,
    configuration,
    token,
}: {
    base: string;
    configuration: number;
    token: string;
}) {
    const fileReadSequence = useRef(0);
    const [preview, setPreview] = useState<Observation[]>([]),
        [error, setError] = useState(""),
        [fileName, setFileName] = useState(""),
        [result, setResult] = useState<{
            created: number;
            existing: number;
            count: number;
        }>();
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Bring your real production into view</h2>
                    <p>
                        Import completed 30 or 60 minute energy observations for
                        this configuration. Only intervals covered by archived
                        estimated actuals can be imported. Check the Calibration
                        journal for available dates.
                    </p>
                </div>
            </div>
            <p>
                Use a CSV with the exact header below. Timestamps need an
                explicit timezone, energy must be non-negative, and intervals
                cannot overlap. Each batch accepts up to 2,000 rows.
            </p>
            <div className="csv-example">
                <code>
                    starts_at,ends_at,energy,quality,source
                    <br />
                    2026-01-01T09:00:00+03:00,2026-01-01T09:30:00+03:00,250,valid,meter
                </code>
            </div>
            <p className="fine">
                Quality: valid, outage, curtailed, or invalid. Quoted CSV fields
                may contain commas and line breaks. Units apply to the whole
                batch. Exact replays are safe; conflicting records reject the
                whole batch.
            </p>
            <ActionForm
                submit={`Import ${preview.length || ""} observations`}
                onSubmit={async (data) => {
                    if (!preview.length)
                        throw new Error(
                            "Choose and validate a CSV before importing.",
                        );
                    const response = await request<{
                        created: number;
                        existing: number;
                        count: number;
                    }>(
                        `${base}observations/`,
                        "POST",
                        {
                            configuration_id: configuration,
                            unit: data.get("unit"),
                            observations: preview,
                        },
                        token,
                    );
                    setResult(response);
                }}
            >
                <div className="form-grid">
                    <label className="field">
                        Production CSV
                        <input
                            type="file"
                            accept=".csv,text/csv"
                            required
                            onChange={async (event) => {
                                const sequence = ++fileReadSequence.current;
                                setPreview([]);
                                setError("");
                                setResult(undefined);
                                const file = event.target.files?.[0];
                                setFileName(file?.name ?? "");
                                if (!file) return;
                                try {
                                    if (file.size > 2 * 1024 * 1024)
                                        throw new Error(
                                            "Choose a CSV no larger than 2 MiB.",
                                        );
                                    const text = await file.text();
                                    if (sequence !== fileReadSequence.current)
                                        return;
                                    setPreview(parseProduction(text));
                                } catch (error) {
                                    if (sequence !== fileReadSequence.current)
                                        return;
                                    setError((error as Error).message);
                                }
                            }}
                        />
                    </label>
                    <Select label="Energy unit" name="unit">
                        <option value="kWh">kWh — kilowatt-hours</option>
                        <option value="MWh">MWh — megawatt-hours</option>
                    </Select>
                </div>
                {error && <Notice error>{error}</Notice>}
                {preview.length > 0 && (
                    <Notice>
                        {fileName}: {preview.length} valid observations ready.
                        First interval: {preview[0].starts_at}.
                    </Notice>
                )}
            </ActionForm>
            {result && (
                <Notice>
                    Import complete: {result.created} created, {result.existing}{" "}
                    already present, {result.count} total.
                </Notice>
            )}
        </section>
    );
}
function Calibrations({
    base,
    token,
    productionRevision,
}: {
    base: string;
    token: string;
    productionRevision: number;
}) {
    const [version, setVersion] = useState(0),
        [selected, setSelected] = useState<Calibration>(),
        [actionError, setActionError] = useState(""),
        [message, setMessage] = useState(""),
        [working, setWorking] = useState(false);
    const resource = useCollection<Calibration>(
        `${base}calibrations/`,
        version + productionRevision,
    );
    async function action(url: string) {
        setWorking(true);
        setActionError("");
        setMessage("");
        try {
            await request(url, "POST", {}, token);
            setMessage(
                "Calibration selection updated. Refresh the forecast to see the current result.",
            );
            setVersion(version + 1);
        } catch (error) {
            setActionError((error as Error).message);
        } finally {
            setWorking(false);
        }
    }
    return (
        <>
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>Calibration history</h2>
                        <p>
                            Ready fits can be activated explicitly. Older runs
                            may need a new assessment before activation. Reset
                            returns future estimates to the baseline.
                        </p>
                    </div>
                    <div className="button-group">
                        <button
                            className="button secondary"
                            onClick={() => setVersion(version + 1)}
                        >
                            Refresh
                        </button>
                        <button
                            className="button secondary"
                            disabled={working}
                            onClick={() => action(`${base}calibration/reset/`)}
                        >
                            Reset to baseline
                        </button>
                    </div>
                </div>
                {actionError && <Notice error>{actionError}</Notice>}
                {message && <Notice>{message}</Notice>}
                {resource.loading ? (
                    <p role="status">Loading calibration runs…</p>
                ) : resource.error ? (
                    <Notice error>{resource.error}</Notice>
                ) : resource.data?.results.length ? (
                    <div className="table-scroll">
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
                                        </td>
                                        <td>{run.assessment}</td>
                                        <td>{run.factor ?? "—"}</td>
                                        <td>
                                            <button
                                                className="text-button"
                                                onClick={async () => {
                                                    try {
                                                        setSelected(
                                                            await request<Calibration>(
                                                                `${base}calibrations/${run.id}/`,
                                                            ),
                                                        );
                                                    } catch (error) {
                                                        setActionError(
                                                            (error as Error)
                                                                .message,
                                                        );
                                                    }
                                                }}
                                            >
                                                View details
                                            </button>
                                            {run.status === "ready" && (
                                                <button
                                                    className="text-button"
                                                    disabled={working}
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
                    </div>
                ) : (
                    <Empty title="Better estimates start with evidence">
                        Fill the production journal. A candidate is prepared
                        automatically once enough complete paired days are
                        available.
                    </Empty>
                )}
                {selected && (
                    <div className="calibration-detail">
                        <div className="section-heading">
                            <h3>Calibration #{selected.id}</h3>
                            <Tag>{selected.status}</Tag>
                        </div>
                        <p>
                            Assessment: {selected.assessment}. Factor:{" "}
                            {selected.factor ?? "unavailable"}.
                        </p>
                        <CalibrationAssessment run={selected} />
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
        rolling = asRecord(result.rolling_origin),
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
            {typeof rolling.status === "string" && (
                <p className="fine">
                    Rolling-origin diagnostic: {rolling.status};{" "}
                    {Array.isArray(rolling.folds) ? rolling.folds.length : 0}{" "}
                    folds.
                </p>
            )}
            {daylightMetrics && (
                <p className="fine">
                    Metrics use whole meter intervals intersecting daylight, not
                    24-hour totals.
                </p>
            )}
            {Object.keys(candidate).length > 0 && (
                <div className="table-scroll">
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
                </div>
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
