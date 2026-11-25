import {
    csvFilename,
    plantFilename,
    type CsvExport,
    type CsvValue,
} from "./csv";
import { plantTimestamp } from "./format/time";
import type { components } from "./schema";

export type { CsvExport } from "./csv";

type Schemas = components["schemas"];
type Column<Row> = readonly [name: string, value: (row: Row) => CsvValue];

// Every export names its plant; times are written on the plant's wall clock with their offset.
export type PlantRef = { id: number; name: string; timezone: string };

export type ForecastRow = {
    start: string;
    end: string;
    ghi_wm2?: number | null;
    mean_power_mw?: number | null;
    energy_mwh?: number | null;
    quality_flags?: string[];
    source_kinds?: string[];
    affected_mean_power_mw?: number | null;
    affected_energy_mwh?: number | null;
    affected_quality_flags?: string[];
};

function table<Row>(
    filename: string,
    columns: readonly Column<Row>[],
    rows: readonly Row[],
): CsvExport {
    return {
        filename,
        header: columns.map(([name]) => name),
        rows: rows.map((row) => columns.map(([, value]) => value(row))),
    };
}

function plantColumns<Row>(plant: PlantRef): Column<Row>[] {
    return [
        ["plant_id", () => plant.id],
        ["plant_name", () => plant.name],
        ["timezone", () => plant.timezone],
    ];
}

const list = (values: readonly string[] | undefined) =>
    values?.length ? values.join("; ") : null;

export function productionExport(
    plant: PlantRef,
    date: string,
    intervals: readonly Schemas["ProductionIntervalOutput"][],
    kind: string,
): CsvExport {
    const stamp = (value: string) => plantTimestamp(value, plant.timezone);
    return table(
        plantFilename(plant.name, kind, date),
        [
            ["starts_at", (row) => stamp(row.interval.starts_at)],
            ["ends_at", (row) => stamp(row.interval.ends_at)],
            ["energy_mwh", (row) => row.value.energy_mwh],
            ["quality", (row) => row.value.quality],
            ["source", (row) => row.value.source],
            ["timezone", () => plant.timezone],
        ],
        intervals.flatMap((interval) =>
            interval.effective ? [{ interval, value: interval.effective }] : [],
        ),
    );
}

export function outagesExport(
    plant: PlantRef,
    outages: readonly Schemas["IncidentOutput"][],
): CsvExport {
    const stamp = (value: string | null) =>
        plantTimestamp(value, plant.timezone);
    return table(
        csvFilename("outages", plant.name),
        [
            ...plantColumns<Schemas["IncidentOutput"]>(plant),
            ["id", (row) => row.id],
            ["status", (row) => row.status],
            ["kind", (row) => row.kind],
            ["title", (row) => row.title],
            ["starts_at", (row) => stamp(row.starts_at)],
            ["ends_at", (row) => stamp(row.ends_at)],
            ["available_dc_mw", (row) => row.available_dc_mw],
            ["available_inverter_mw", (row) => row.available_inverter_mw],
            ["available_export_mw", (row) => row.available_export_mw],
            ["notes", (row) => row.notes],
        ],
        outages,
    );
}

function reportValue(report: unknown, ...path: string[]): CsvValue {
    const value = path.reduce<unknown>(
        (node, key) =>
            node && typeof node === "object" && Object.hasOwn(node, key)
                ? Reflect.get(node, key)
                : null,
        report,
    );
    return typeof value === "number" || typeof value === "boolean"
        ? value
        : null;
}

type Run = Schemas["CalibrationSummaryOutput"];

function runColumns(plant: PlantRef): Column<Run>[] {
    const stamp = (value: string | null) =>
        plantTimestamp(value, plant.timezone);
    return [
        ...plantColumns<Run>(plant),
        ["id", (run) => run.id],
        ["window_start", (run) => run.window_start],
        ["window_end", (run) => run.window_end],
        ["status", (run) => run.status],
        ["reason", (run) => run.reason],
        ["selection_state", (run) => run.selection_state],
        ["assessment", (run) => run.assessment || null],
        ["factor", (run) => run.factor],
        [
            "provisional_production",
            (run) => reportValue(run.result, "provisional_production"),
        ],
        ["created_at", (run) => stamp(run.created_at)],
        ["completed_at", (run) => stamp(run.completed_at)],
    ];
}

export function calibrationsExport(
    plant: PlantRef,
    runs: readonly Run[],
    kind: string,
): CsvExport {
    return table(plantFilename(plant.name, kind), runColumns(plant), runs);
}

export function calibrationExport(
    plant: PlantRef,
    run: Schemas["CalibrationOutput"],
    kind: string,
): CsvExport {
    const report = (...path: string[]) => reportValue(run.result, ...path);
    return table(
        plantFilename(plant.name, kind),
        [
            ...runColumns(plant),
            ["cutoff_at", () => plantTimestamp(run.cutoff_at, plant.timezone)],
            ["activation_blocker", () => run.activation_blocker],
            ["training_days", () => report("training_days")],
            ["holdout_days", () => report("holdout_days")],
            ["valid_days", () => report("valid_days")],
            [
                "baseline_daily_mae_mwh",
                () => report("baseline", "daily_mae_mwh"),
            ],
            [
                "candidate_daily_mae_mwh",
                () => report("candidate", "daily_mae_mwh"),
            ],
            [
                "baseline_interval_mae_mw",
                () => report("baseline", "interval_mae_mw"),
            ],
            [
                "candidate_interval_mae_mw",
                () => report("candidate", "interval_mae_mw"),
            ],
        ],
        [run],
    );
}

type Day = Pick<
    Schemas["PerformanceDay"],
    | "date"
    | "status"
    | "reason"
    | "source_kind"
    | "measured_energy_mwh"
    | "predicted_energy_mwh"
    | "error_mwh"
    | "provisional_production"
    | "calibration_in_sample"
>;

export type PerformanceFileNames = {
    file: string;
    comparison: string;
    forecast: string;
    satellite: string;
};

export function performanceExport(
    plant: PlantRef,
    performance: Pick<
        Schemas["PerformanceOutput"],
        "start_date" | "end_date"
    > & { days: readonly Day[] },
    names: PerformanceFileNames,
): CsvExport {
    return table(
        plantFilename(
            plant.name,
            names.file,
            performance.start_date,
            performance.end_date,
        ),
        [
            ...plantColumns<Day>(plant),
            ["comparison", () => names.comparison],
            ["date", (day) => day.date],
            ["status", (day) => day.status],
            ["reason", (day) => day.reason],
            [
                "source_kind",
                (day) =>
                    day.source_kind === null
                        ? null
                        : day.source_kind === "estimated_actuals"
                          ? names.satellite
                          : names.forecast,
            ],
            ["measured_energy_mwh", (day) => day.measured_energy_mwh],
            ["predicted_energy_mwh", (day) => day.predicted_energy_mwh],
            ["error_mwh", (day) => day.error_mwh],
            ["provisional_production", (day) => day.provisional_production],
            ["calibration_in_sample", (day) => day.calibration_in_sample],
        ],
        performance.days,
    );
}

function forecastValues(
    electrical: boolean,
    scenario: boolean,
): Column<ForecastRow>[] {
    if (!electrical)
        return [
            ["ghi_wm2", (row) => row.ghi_wm2 ?? null],
            ["quality_flags", (row) => list(row.quality_flags)],
        ];
    if (!scenario)
        return [
            ["mean_power_mw", (row) => row.mean_power_mw ?? null],
            ["energy_mwh", (row) => row.energy_mwh ?? null],
            ["quality_flags", (row) => list(row.quality_flags)],
        ];
    return [
        ["normal_mean_power_mw", (row) => row.mean_power_mw ?? null],
        ["normal_energy_mwh", (row) => row.energy_mwh ?? null],
        ["normal_quality_flags", (row) => list(row.quality_flags)],
        ["affected_mean_power_mw", (row) => row.affected_mean_power_mw ?? null],
        ["affected_energy_mwh", (row) => row.affected_energy_mwh ?? null],
        ["affected_quality_flags", (row) => list(row.affected_quality_flags)],
    ];
}

export type ForecastFileNames = {
    file: string;
    forecast: string;
    satellite: string;
};

export function forecastExport(
    plant: PlantRef,
    forecast: { date?: string; source_kind?: string | null },
    rows: readonly ForecastRow[],
    {
        electrical,
        scenario,
        names,
    }: { electrical: boolean; scenario: boolean; names: ForecastFileNames },
): CsvExport {
    const stamp = (value: string) => plantTimestamp(value, plant.timezone);
    const kind = (kinds: readonly string[] | undefined) =>
        !kinds?.length
            ? null
            : kinds.every((value) => value === "estimated_actuals")
              ? names.satellite
              : names.forecast;
    const source: Column<ForecastRow>[] =
        forecast.source_kind === "mixed"
            ? [["interval_source", (row) => kind(row.source_kinds)]]
            : [];
    return table(
        plantFilename(plant.name, names.file, forecast.date ?? ""),
        [
            ...plantColumns<ForecastRow>(plant),
            ["date", () => forecast.date ?? null],
            [
                "source_kind",
                () => kind(forecast.source_kind ? [forecast.source_kind] : []),
            ],
            ["starts_at", (row) => stamp(row.start)],
            ["ends_at", (row) => stamp(row.end)],
            ...forecastValues(electrical, scenario),
            ...source,
        ],
        rows,
    );
}

type PlantSettings = Pick<
    Schemas["ConfigurationOutput"],
    | "dc_capacity_mw"
    | "inverter_capacity_mw"
    | "export_limit_mw"
    | "measurement_basis"
    | "mounting_type"
    | "surface_tilt_degrees"
    | "surface_azimuth_degrees"
> & { revision?: number };

export function plantExport(
    plant: PlantRef,
    location: Pick<Schemas["RegionOutput"], "latitude" | "longitude">,
    settings: PlantSettings | null,
): CsvExport {
    return table(
        csvFilename("plant", plant.name),
        [
            ...plantColumns<null>(plant),
            ["latitude", () => location.latitude],
            ["longitude", () => location.longitude],
            ["configuration_revision", () => settings?.revision ?? null],
            ["dc_capacity_mw", () => settings?.dc_capacity_mw ?? null],
            [
                "inverter_capacity_mw",
                () => settings?.inverter_capacity_mw ?? null,
            ],
            ["export_limit_mw", () => settings?.export_limit_mw ?? null],
            ["measurement_basis", () => settings?.measurement_basis ?? null],
            ["mounting_type", () => settings?.mounting_type ?? null],
            [
                "surface_tilt_degrees",
                () => settings?.surface_tilt_degrees ?? null,
            ],
            [
                "surface_azimuth_degrees",
                () => settings?.surface_azimuth_degrees ?? null,
            ],
        ],
        [null],
    );
}
