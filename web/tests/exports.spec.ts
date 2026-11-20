import { expect, test } from "@playwright/test";
import { csvDocument, csvFilename } from "../src/lib/csv";
import {
    calibrationExport,
    calibrationsExport,
    forecastExport,
    outagesExport,
    performanceExport,
    plantExport,
    productionExport,
    type CsvExport,
} from "../src/lib/exports";
import { parseProduction } from "../src/lib/production";
import type { components } from "../src/lib/schema";

type Schemas = components["schemas"];

const plant = { id: 21, name: "Doğu: array", timezone: "Europe/Istanbul" };

function lines(file: CsvExport) {
    return csvDocument(file.header, file.rows)
        .replace(/^\uFEFF/, "")
        .split("\r\n");
}

const interval = (
    starts_at: string,
    ends_at: string,
    energy_mwh: number | null,
): Schemas["ProductionIntervalOutput"] => ({
    starts_at,
    ends_at,
    observation: null,
    previous_configuration_observations: [],
    revision_id: null,
    effective:
        energy_mwh === null
            ? null
            : {
                  energy_mwh,
                  quality: "valid",
                  source: "meter",
                  source_kind: "manual",
                  provisional: false,
                  aggregated: false,
              },
    provider_originals: [
        {
            starts_at,
            ends_at,
            energy_mwh: 9,
            source_kind: "realtime_generation",
            snapshot_id: 3,
            collected_at: ends_at,
            available_at: ends_at,
            content_sha256: "abc",
            provisional: false,
        },
    ],
    can_edit: true,
    can_revert: false,
    edit_unavailable_reason: null,
    can_propose: false,
    can_propose_revert: false,
    proposal_unavailable_reason: null,
});

const outage: Schemas["IncidentOutput"] = {
    id: 51,
    revision: 2,
    kind: "inverter",
    title: '=HYPERLINK("x"), "quoted"',
    notes: "line one\nline two",
    starts_at: "2026-11-11T08:00:00Z",
    ends_at: null,
    available_dc_mw: 0,
    available_inverter_mw: null,
    available_export_mw: 1.5,
    declared_configuration_id: 31,
    status: "active",
    created_at: "2026-11-11T12:00:00Z",
    updated_at: "2026-11-11T12:00:00Z",
};

test("every export names the plant safely in its file name", () => {
    expect(csvFilename("production", plant.name, "2026-11-10")).toBe(
        "production-Doğu- array-2026-11-10.csv",
    );
    expect(csvFilename("outages", ' ..con<>|?*"/\\ ')).toBe("outages-con.csv");
});

test("cells quote text, double quotes and never start a spreadsheet formula", () => {
    const [header, row] = csvDocument(
        ["a", "b", "c", "d", "e", "f", "g"],
        [["=1+1", "\u200b@x", "\t-cmd", "-2.5", "a,b", 'say "hi"', null]],
    )
        .replace(/^\uFEFF/, "")
        .split("\r\n");
    expect(header).toBe('"a","b","c","d","e","f","g"');
    expect(row).toBe(
        `"'=1+1","'\u200b@x","'\t-cmd","-2.5","a,b","say ""hi""",`,
    );
});

test("a long run of leading invisible characters still ends in the formula check", () => {
    const padding = " \t200b".repeat(50_000);
    const [, row] = csvDocument(
        ["a"],
        [[`${padding}=1+1`], [`${padding}text`], [padding]],
    )
        .replace(/^\uFEFF/, "")
        .split("\r\n");
    expect(row).toBe(`"'${padding}=1+1"`);
});

test("only a formula character after invisible ones is neutralised", () => {
    const lines = csvDocument(["a"], [[" \u00ad+x"], ["x=1"], ["\u00a0@"]])
        .replace(/^\ufeff/, "")
        .split("\r\n");
    expect(lines.slice(1, 4)).toEqual([`"' \u00ad+x"`, '"x=1"', `"'\u00a0@"`]);
});

test("the production export holds the import columns and one row per recorded interval in plant time", () => {
    const file = productionExport(plant, "2026-11-10", [
        interval("2026-11-10T05:00:00Z", "2026-11-10T05:30:00Z", 0.5),
        interval("2026-11-10T05:30:00Z", "2026-11-10T06:00:00Z", null),
    ]);
    expect(file.filename).toBe("production-Doğu- array-2026-11-10.csv");
    expect(lines(file)).toEqual([
        '"starts_at","ends_at","energy_mwh","quality","source","timezone"',
        '"2026-11-10T08:00:00+03:00","2026-11-10T08:30:00+03:00",0.5,"valid","meter","Europe/Istanbul"',
        "",
    ]);
});

test("a production export is imported again without edits", () => {
    const file = productionExport(plant, "2026-11-10", [
        interval("2026-11-10T05:00:00Z", "2026-11-10T05:30:00Z", 0.5),
        interval("2026-11-10T05:30:00Z", "2026-11-10T06:00:00Z", 0.25),
    ]);
    const parsed = parseProduction(csvDocument(file.header, file.rows));
    expect(parsed.unit).toBe("MWh");
    expect(parsed.observations).toEqual([
        {
            starts_at: "2026-11-10T08:00:00+03:00",
            ends_at: "2026-11-10T08:30:00+03:00",
            energy: 0.5,
            quality: "valid",
            source: "meter",
        },
        {
            starts_at: "2026-11-10T08:30:00+03:00",
            ends_at: "2026-11-10T09:00:00+03:00",
            energy: 0.25,
            quality: "valid",
            source: "meter",
        },
    ]);
});

test("an empty list gives the header and no fabricated row", () => {
    for (const file of [
        productionExport(plant, "2026-11-10", []),
        outagesExport(plant, []),
        calibrationsExport(plant, []),
    ])
        expect(lines(file)).toHaveLength(2);
});

test("the outage export states plant time and keeps typed text inert", () => {
    const file = outagesExport(plant, [outage]);
    expect(file.filename).toBe("outages-Doğu- array.csv");
    expect(lines(file).slice(0, 2)).toEqual([
        '"plant_id","plant_name","timezone","id","status","kind","title","starts_at","ends_at","available_dc_mw","available_inverter_mw","available_export_mw","notes"',
        `21,"Doğu: array","Europe/Istanbul",51,"active","inverter","'=HYPERLINK(""x""), ""quoted""","2026-11-11T11:00:00+03:00",,0,,1.5,"line one\nline two"`,
    ]);
});

const run: Schemas["CalibrationOutput"] = {
    id: 40,
    configuration_id: 31,
    binding_id: 7,
    requested_by_id: 1,
    window_start: "2026-01-01",
    window_end: "2026-01-30",
    cutoff_at: "2026-01-31T00:00:00Z",
    created_at: "2026-01-31T01:00:00Z",
    completed_at: "2026-01-31T02:00:00Z",
    status: "ready",
    reason: null,
    selection_state: "candidate",
    assessment: "evaluated",
    factor: 1.1,
    result: {
        provisional_production: false,
        training_days: 20,
        holdout_days: 7,
        valid_days: 27,
        baseline: { daily_mae_mwh: 2, interval_mae_mw: 0.4 },
        candidate: { daily_mae_mwh: 1.5, interval_mae_mw: 0.3 },
    },
    dataset_sha256: "f00",
    activation_blocker: null,
    dataset_manifest: { provider: "secret" },
};

test("calibration exports list each run with its selection and no provider data", () => {
    const list = calibrationsExport(plant, [run]);
    expect(list.filename).toBe("calibrations-Doğu- array.csv");
    expect(lines(list).slice(0, 2)).toEqual([
        '"plant_id","plant_name","timezone","id","window_start","window_end","status","reason","selection_state","assessment","factor","provisional_production","created_at","completed_at"',
        '21,"Doğu: array","Europe/Istanbul",40,"2026-01-01","2026-01-30","ready",,"candidate","evaluated",1.1,false,"2026-01-31T04:00:00+03:00","2026-01-31T05:00:00+03:00"',
    ]);
    const detail = calibrationExport(plant, run);
    expect(detail.filename).toBe("calibration-40-Doğu- array.csv");
    expect(lines(detail).slice(0, 2)).toEqual([
        '"plant_id","plant_name","timezone","id","window_start","window_end","status","reason","selection_state","assessment","factor","provisional_production","created_at","completed_at","cutoff_at","activation_blocker","training_days","holdout_days","valid_days","baseline_daily_mae_mwh","candidate_daily_mae_mwh","baseline_interval_mae_mw","candidate_interval_mae_mw"',
        '21,"Doğu: array","Europe/Istanbul",40,"2026-01-01","2026-01-30","ready",,"candidate","evaluated",1.1,false,"2026-01-31T04:00:00+03:00","2026-01-31T05:00:00+03:00","2026-01-31T03:00:00+03:00",,20,7,27,2,1.5,0.4,0.3',
    ]);
    expect(csvDocument(detail.header, detail.rows)).not.toContain("secret");
});

test("the performance export keeps the plant, zone, source and model check per day", () => {
    const file = performanceExport(plant, {
        comparison: "historical_model",
        start_date: "2026-11-01",
        end_date: "2026-11-02",
        days: [
            {
                date: "2026-11-01",
                status: "matched",
                reason: null,
                source_kind: "estimated_actuals",
                measured_energy_mwh: 10,
                predicted_energy_mwh: 11,
                error_mwh: 1,
                provisional_production: false,
                calibration_in_sample: true,
            },
        ],
    });
    expect(file.filename).toBe(
        "performance-Doğu- array-2026-11-01-2026-11-02.csv",
    );
    expect(lines(file).slice(0, 2)).toEqual([
        '"plant_id","plant_name","timezone","comparison","date","status","reason","source_kind","measured_energy_mwh","predicted_energy_mwh","error_mwh","provisional_production","calibration_in_sample"',
        '21,"Doğu: array","Europe/Istanbul","historical_model","2026-11-01","matched",,"estimated_actuals",10,11,1,false,true',
    ]);
});

test("the forecast export writes one row per interval with its unit and source", () => {
    const forecast = { date: "2026-11-12", source_kind: "forecast" as const };
    const row = {
        start: "2026-11-12T08:00:00Z",
        end: "2026-11-12T09:00:00Z",
        mean_power_mw: 2,
        energy_mwh: 2,
        quality_flags: ["clipped", "estimated"],
        affected_mean_power_mw: 0,
        affected_energy_mwh: 0,
        affected_quality_flags: [],
    };
    const electrical = forecastExport(plant, forecast, [row], {
        electrical: true,
        scenario: false,
    });
    expect(electrical.filename).toBe(
        "forecast-Doğu- array-2026-11-12-forecast.csv",
    );
    expect(lines(electrical).slice(0, 2)).toEqual([
        '"plant_id","plant_name","timezone","date","source_kind","starts_at","ends_at","mean_power_mw","energy_mwh","quality_flags"',
        '21,"Doğu: array","Europe/Istanbul","2026-11-12","forecast","2026-11-12T11:00:00+03:00","2026-11-12T12:00:00+03:00",2,2,"clipped; estimated"',
    ]);
    const scenario = forecastExport(plant, forecast, [row], {
        electrical: true,
        scenario: true,
    });
    expect(lines(scenario)[0]).toBe(
        '"plant_id","plant_name","timezone","date","source_kind","starts_at","ends_at","normal_mean_power_mw","normal_energy_mwh","normal_quality_flags","affected_mean_power_mw","affected_energy_mwh","affected_quality_flags"',
    );
    const irradiance = forecastExport(
        plant,
        forecast,
        [{ ...row, ghi_wm2: 410 }],
        { electrical: false, scenario: false },
    );
    expect(lines(irradiance)[0]).toBe(
        '"plant_id","plant_name","timezone","date","source_kind","starts_at","ends_at","ghi_wm2","quality_flags"',
    );
    expect(irradiance.filename).toBe(
        "irradiance-Doğu- array-2026-11-12-forecast.csv",
    );
});

test("the plant export lists the configuration and location", () => {
    const file = plantExport(
        plant,
        { latitude: 36.9, longitude: 30.7 },
        {
            revision: 2,
            dc_capacity_mw: 5,
            inverter_capacity_mw: 4,
            export_limit_mw: null,
            measurement_basis: "net_export",
            mounting_type: "fixed",
            surface_tilt_degrees: 25,
            surface_azimuth_degrees: 180,
        },
    );
    expect(file.filename).toBe("plant-Doğu- array.csv");
    expect(lines(file).slice(0, 2)).toEqual([
        '"plant_id","plant_name","timezone","latitude","longitude","configuration_revision","dc_capacity_mw","inverter_capacity_mw","export_limit_mw","measurement_basis","mounting_type","surface_tilt_degrees","surface_azimuth_degrees"',
        '21,"Doğu: array","Europe/Istanbul",36.9,30.7,2,5,4,,"net_export","fixed",25,180',
    ]);
});
