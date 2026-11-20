import { expect, test } from "@playwright/test";
import { csvDocument } from "../src/lib/csv";
import { productionCsvRecords } from "../src/features/production/history/model";

function lines(document: string) {
    return document.replace(/^﻿/, "").split("\r\n");
}

const outage = {
    id: 51,
    revision: 2,
    kind: "inverter",
    title: "Inverter",
    notes: "",
    starts_at: "2026-11-11T08:00:00Z",
    ends_at: null,
    available_dc_mw: 0,
    available_inverter_mw: 0,
    available_export_mw: 0,
    declared_configuration_id: 31,
    status: "active",
    created_at: "2026-11-11T12:00:00Z",
    updated_at: "2026-11-11T12:00:00Z",
};

test("the encoder writes a byte order mark, CRLF rows, quoted text and empty missing values", () => {
    const document = csvDocument([
        { a: 1, b: "x", c: null },
        { a: true, b: 'say "hi", then', c: "" },
    ]);
    expect(document.startsWith("﻿")).toBe(true);
    expect(lines(document)).toEqual([
        '"a","b","c"',
        '1,"x",',
        'true,"say ""hi"", then",""',
        "",
    ]);
});

test("the encoder neutralises leading formula characters", () => {
    expect(
        lines(csvDocument([{ a: "=1+1", b: " @x", c: "-2", d: "\t+3" }]))[1],
    ).toBe(`"'=1+1","' @x","'-2","'\t+3"`);
});

test("an empty record list gives one row of metadata", () => {
    expect(lines(csvDocument([], { timezone: "Europe/Istanbul" }))).toEqual([
        '"timezone"',
        '"Europe/Istanbul"',
        "",
    ]);
});

test("the outage export flattens each list row after the device and plant zones", () => {
    const [header, row] = lines(
        csvDocument([outage], {
            timezone: "UTC",
            plant_timezone: "Europe/Istanbul",
        }),
    );
    expect(header).toBe(
        '"timezone","plant_timezone","id","revision","kind","title","notes","starts_at","ends_at","available_dc_mw","available_inverter_mw","available_export_mw","declared_configuration_id","status","created_at","updated_at"',
    );
    expect(row).toBe(
        '"UTC","Europe/Istanbul",51,2,"inverter","Inverter","","2026-11-11T08:00:00Z",,0,0,0,31,"active","2026-11-11T12:00:00Z","2026-11-11T12:00:00Z"',
    );
});

test("the production export writes the effective value and provider originals per interval", () => {
    const [header, row] = lines(
        csvDocument(
            productionCsvRecords([
                {
                    starts_at: "2026-11-10T05:00:00Z",
                    ends_at: "2026-11-10T05:30:00Z",
                    observation: null,
                    previous_configuration_observations: [],
                    revision_id: 7,
                    effective: {
                        energy_mwh: 0.5,
                        quality: "valid",
                        source: "meter",
                        source_kind: "manual",
                        provisional: false,
                        aggregated: false,
                    },
                    provider_originals: [],
                    can_edit: true,
                    can_revert: false,
                    edit_unavailable_reason: null,
                    can_propose: false,
                    can_propose_revert: false,
                    proposal_unavailable_reason: null,
                },
            ]),
            { timezone: "Europe/Istanbul", unit: "MWh" },
        ),
    );
    expect(header).toBe(
        '"timezone","unit","starts_at","ends_at","energy","quality","source","provider_energy_mwh","provider_intervals","provider_source"',
    );
    expect(row).toBe(
        '"Europe/Istanbul","MWh","2026-11-10T05:00:00Z","2026-11-10T05:30:00Z",0.5,"valid","meter","","",""',
    );
});

test("the plant export flattens the plant record after its zone", () => {
    const [header] = lines(
        csvDocument(
            [
                {
                    id: 21,
                    name: "East array",
                    current_configuration: { id: 31, dc_capacity_mw: 5 },
                },
            ],
            { timezone: "Europe/Istanbul" },
        ),
    );
    expect(header).toBe(
        '"timezone","id","name","current_configuration.id","current_configuration.dc_capacity_mw"',
    );
});

test("the calibration, performance and forecast exports take their columns from the response", () => {
    const [calibration] = lines(
        csvDocument([{ id: 40, status: "ready", result: { holdout_days: 3 } }]),
    );
    expect(calibration).toBe('"id","status","result.holdout_days"');
    const [performance] = lines(
        csvDocument([{ date: "2026-11-01", status: "matched" }], {
            plant_id: 21,
            comparison: "archived_forecast",
            days: undefined,
            timezone: "Europe/Istanbul",
        }),
    );
    expect(performance).toBe(
        '"plant_id","comparison","timezone","date","status"',
    );
    const [forecast] = lines(
        csvDocument([{ start: "2026-11-12T08:00:00Z", mean_power_mw: 2 }], {
            date: "2026-11-12",
            timezone: "Europe/Istanbul",
            result: { energy_mwh: 2, intervals: undefined },
        }),
    );
    expect(forecast).toBe(
        '"date","timezone","result.energy_mwh","start","mean_power_mw"',
    );
});
