import {
    plantFilename,
    type CsvCell,
    type CsvColumn,
    type CsvExport,
    type CsvText,
    type CsvWord,
} from "./csv";
import { plantTimestamp } from "./format/time";
import { productionSource, qualityOf } from "./production";
import type { components } from "./schema";

export type { CsvExport } from "./csv";

type Schemas = components["schemas"];
type Column<Row> = readonly [name: CsvColumn, value: (row: Row) => CsvCell];

// Times are written on the plant's wall clock with their offset.
export type PlantRef = { id: number; name: string; timezone: string };

export type ForecastRow = {
    start: string;
    end: string;
    ghi_wm2?: number | null;
    mean_power_mw?: number | null;
    energy_mwh?: number | null;
    source_kinds?: string[];
    affected_mean_power_mw?: number | null;
    affected_energy_mwh?: number | null;
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

const word = (word: CsvWord): CsvText => ({ kind: "word", word });

function plantColumn<Row>(plant: PlantRef): Column<Row> {
    return ["plant", () => plant.name];
}

function timeColumns<Row>(
    plant: PlantRef,
    start: (row: Row) => string | null,
    end: (row: Row) => string | null,
): Column<Row>[] {
    return [
        ["start", (row) => plantTimestamp(start(row), plant.timezone)],
        ["end", (row) => plantTimestamp(end(row), plant.timezone)],
    ];
}

type ProductionRow = {
    interval: Schemas["ProductionIntervalOutput"];
    value: Schemas["ProductionValueOutput"];
};

function savedQuality({ interval, value }: ProductionRow) {
    return (
        interval.observation?.quality ??
        interval.provider_originals.find(
            (original) => original.source_kind === value.source_kind,
        )?.quality ??
        value.quality
    );
}

export function productionExport(
    plant: PlantRef,
    date: string,
    intervals: readonly Schemas["ProductionIntervalOutput"][],
    kind: string,
): CsvExport {
    type Row = ProductionRow;
    return table<Row>(
        plantFilename(plant.name, kind, date),
        [
            ...timeColumns<Row>(
                plant,
                (row) => row.interval.starts_at,
                (row) => row.interval.ends_at,
            ),
            ["actual_mwh", (row) => row.value.energy_mwh],
            [
                "status",
                (row) =>
                    row.value.quality === "mixed"
                        ? word("mixed")
                        : {
                              kind: "label",
                              group: "ObservationQuality",
                              value: qualityOf(savedQuality(row)),
                          },
            ],
            [
                "source",
                ({ interval }) =>
                    interval.observation
                        ? interval.observation.source
                        : word(productionSource(interval)),
            ],
        ],
        intervals.flatMap((interval) =>
            interval.effective ? [{ interval, value: interval.effective }] : [],
        ),
    );
}

export function outagesExport(
    plant: PlantRef,
    outages: readonly Schemas["IncidentOutput"][],
    kind: string,
): CsvExport {
    type Row = Schemas["IncidentOutput"];
    return table<Row>(
        plantFilename(plant.name, kind),
        [
            plantColumn(plant),
            ["title", (row) => row.title],
            [
                "type",
                (row) => ({
                    kind: "label",
                    group: "IncidentKind",
                    value: row.kind,
                }),
            ],
            [
                "status",
                (row) => ({
                    kind: "label",
                    group: "IncidentStatus",
                    value: row.status,
                }),
            ],
            ...timeColumns<Row>(
                plant,
                (row) => row.starts_at,
                (row) => row.ends_at,
            ),
            ["panels_in_service_mw", (row) => row.available_dc_mw],
            ["inverter_in_service_mw", (row) => row.available_inverter_mw],
            ["export_in_service_mw", (row) => row.available_export_mw],
            ["notes", (row) => row.notes],
        ],
        outages,
    );
}

function reportValue(report: unknown, ...path: string[]): CsvCell {
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

function runColumns(plant: PlantRef, inUse: number | null): Column<Run>[] {
    return [
        plantColumn(plant),
        ["number", (run) => run.id],
        ["period_start", (run) => run.window_start],
        ["period_end", (run) => run.window_end],
        [
            "status",
            (run) => ({
                kind: "label",
                group: "CalibrationStatus",
                value: run.status,
            }),
        ],
        ["in_use", (run) => run.id === inUse],
        ["preliminary", (run) => run.assessment === "provisional"],
        [
            "realtime",
            (run) => reportValue(run.result, "provisional_production"),
        ],
        ["factor", (run) => run.factor],
    ];
}

export function calibrationsExport(
    plant: PlantRef,
    runs: readonly Run[],
    kind: string,
    inUse: number | null,
): CsvExport {
    return table(
        plantFilename(plant.name, kind),
        runColumns(plant, inUse),
        runs,
    );
}

export function calibrationExport(
    plant: PlantRef,
    run: Schemas["CalibrationOutput"],
    kind: string,
    inUse: boolean,
): CsvExport {
    const report = (...path: string[]) => reportValue(run.result, ...path);
    return table(
        plantFilename(plant.name, kind),
        [
            ...runColumns(plant, inUse ? run.id : null),
            ["calibration_days", () => report("training_days")],
            ["test_days", () => report("holdout_days")],
            ["error_without_mwh", () => report("baseline", "daily_mae_mwh")],
            ["error_with_mwh", () => report("candidate", "daily_mae_mwh")],
        ],
        [run],
    );
}

type Day = Pick<
    Schemas["PerformanceDay"],
    | "date"
    | "status"
    | "reason"
    | "measured_energy_mwh"
    | "predicted_energy_mwh"
    | "error_mwh"
    | "provisional_production"
>;

export type PerformanceFileNames = {
    file: string;
    comparison: string;
};

export function performanceExport(
    plant: PlantRef,
    performance: Pick<
        Schemas["PerformanceOutput"],
        "start_date" | "end_date" | "data_source"
    > & { days: readonly Day[] },
    names: PerformanceFileNames,
): CsvExport {
    const satellite = performance.data_source === "satellite";
    return table<Day>(
        plantFilename(
            plant.name,
            names.file,
            performance.start_date,
            performance.end_date,
        ),
        [
            plantColumn(plant),
            ["comparison", () => names.comparison],
            ["date", (day) => day.date],
            [
                "status",
                (day) =>
                    word(
                        day.status === "matched" ? "compared" : "not_compared",
                    ),
            ],
            [
                "reason",
                (day) =>
                    day.status === "matched"
                        ? null
                        : { kind: "reason", reason: day.reason },
            ],
            ["actual_mwh", (day) => day.measured_energy_mwh],
            [
                satellite ? "satellite_mwh" : "forecast_mwh",
                (day) => day.predicted_energy_mwh,
            ],
            [
                satellite
                    ? "satellite_difference_mwh"
                    : "forecast_difference_mwh",
                (day) => day.error_mwh,
            ],
            ["realtime", (day) => day.provisional_production],
        ],
        performance.days,
    );
}

function forecastValues(
    electrical: boolean,
    scenario: boolean,
    satellite: boolean,
): Column<ForecastRow>[] {
    if (!electrical) return [["irradiance", (row) => row.ghi_wm2 ?? null]];
    if (!scenario)
        return [
            [
                satellite ? "energy_mwh" : "forecast_mwh",
                (row) => row.energy_mwh ?? null,
            ],
            ["power_mw", (row) => row.mean_power_mw ?? null],
        ];
    return [
        ["without_outage_mwh", (row) => row.energy_mwh ?? null],
        ["with_outage_mwh", (row) => row.affected_energy_mwh ?? null],
        ["power_without_outage_mw", (row) => row.mean_power_mw ?? null],
        ["power_with_outage_mw", (row) => row.affected_mean_power_mw ?? null],
    ];
}

export type ForecastFileNames = {
    file: string;
    forecast: string;
    satellite: string;
};

// ISO 8601 basic form on the plant's wall clock, such as 20261110T1700.
function fileTime(value: string | undefined, timeZone: string): string {
    return (
        plantTimestamp(value, timeZone)?.slice(0, 16).replaceAll(/[-:]/g, "") ??
        ""
    );
}

export function forecastExport(
    plant: PlantRef,
    forecast: { date?: string; source_kind?: string | null },
    rows: readonly ForecastRow[],
    {
        electrical,
        scenario,
        names,
        fetchedAt,
    }: {
        electrical: boolean;
        scenario: boolean;
        names: ForecastFileNames;
        fetchedAt?: string;
    },
): CsvExport {
    const kind = (kinds: readonly string[] | undefined) =>
        !kinds?.length
            ? null
            : kinds.every((value) => value === "estimated_actuals")
              ? names.satellite
              : names.forecast;
    const day = forecast.source_kind ? [forecast.source_kind] : [];
    return table<ForecastRow>(
        plantFilename(
            plant.name,
            names.file,
            forecast.date ?? "",
            fileTime(fetchedAt, plant.timezone),
        ),
        [
            plantColumn(plant),
            [
                "data",
                (row) =>
                    kind(
                        forecast.source_kind === "mixed"
                            ? row.source_kinds
                            : day,
                    ),
            ],
            ...timeColumns<ForecastRow>(
                plant,
                (row) => row.start,
                (row) => row.end,
            ),
            ...forecastValues(
                electrical,
                scenario,
                forecast.source_kind === "estimated_actuals",
            ),
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
>;

export function plantExport(
    plant: PlantRef,
    location: Pick<Schemas["RegionOutput"], "latitude" | "longitude">,
    settings: PlantSettings | null,
    kind: string,
): CsvExport {
    return table<null>(
        plantFilename(plant.name, kind),
        [
            plantColumn(plant),
            ["latitude", () => location.latitude],
            ["longitude", () => location.longitude],
            ["time_zone", () => plant.timezone],
            ["panel_capacity_mwp", () => settings?.dc_capacity_mw ?? null],
            [
                "inverter_capacity_mw",
                () => settings?.inverter_capacity_mw ?? null,
            ],
            ["export_limit_mw", () => settings?.export_limit_mw ?? null],
            ["meter_point", () => settings && word(settings.measurement_basis)],
            ["mounting", () => settings && word(settings.mounting_type)],
            ["tilt", () => settings?.surface_tilt_degrees ?? null],
            ["azimuth", () => settings?.surface_azimuth_degrees ?? null],
        ],
        [null],
    );
}
