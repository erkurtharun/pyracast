import { expect, test } from "@playwright/test";
import {
    csvDocument,
    csvFilename,
    plantFilename,
    readable,
    type CsvExport,
} from "../src/lib/csv";
import {
    calibrationExport,
    calibrationsExport,
    forecastExport,
    outagesExport,
    performanceExport,
    plantExport,
    productionExport,
} from "../src/lib/exports";
import { parseProduction } from "../src/lib/production";
import type { Language } from "../src/i18n/language";
import { csvTexts, fileTexts } from "./support/production-csv";
import type { components } from "../src/lib/schema";

type Schemas = components["schemas"];

const plant = { id: 21, name: "Doğu: array", timezone: "Europe/Istanbul" };

function written(file: CsvExport, language: Language = "en") {
    const { header, rows } = readable(file, fileTexts(language));
    return csvDocument(header, rows);
}

function lines(file: CsvExport, language: Language = "en") {
    return written(file, language)
        .replace(/^\uFEFF/, "")
        .split("\r\n");
}

const interval = (
    starts_at: string,
    ends_at: string,
    energy_mwh: number | null,
    source_kind = "manual",
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
                  source_kind,
                  provisional: source_kind === "realtime_generation",
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
    pending_request_id: null,
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

test("a long run of spaces, dots and hyphens is trimmed only at the name edges", () => {
    const run = " .-\t".repeat(12_500);
    expect(csvFilename("outages", `${run}plant${run}`, run)).toBe(
        "outages-plant.csv",
    );
    expect(csvFilename("outages", `${run}plant${run}x`)).toBe(
        `outages-plant${" .--".repeat(12_500)}x`.slice(0, 150) + ".csv",
    );
});

test("a plant file name shortens the plant and keeps every date and the kind", () => {
    const dates = Array.from({ length: 10 }, (_, day) => `2026-11-1${day}`);
    const name = plantFilename("Alpha ".repeat(40), "comparison", ...dates);
    expect(name).toHaveLength(154);
    expect(name).toBe(
        `pyracast-alpha-alpha-alpha-al-${dates.join("-")}-comparison.csv`,
    );
});

test("a plant file name whose dates and kind leave no room for the plant is refused", () => {
    const dates = Array.from({ length: 13 }, (_, day) => `2026-11-${day + 10}`);
    expect(() => plantFilename("Alpha", "comparison", ...dates)).toThrow(
        "The file kind and dates leave no room for the plant.",
    );
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
    const padding = " \t\u200B".repeat(50_000);
    const lines = csvDocument(
        ["a"],
        [[`${padding}=1+1`], [`${padding}text`], [padding]],
    )
        .replace(/^\uFEFF/, "")
        .split("\r\n");
    expect(lines.slice(1, 4)).toEqual([
        `"'${padding}=1+1"`,
        `"${padding}text"`,
        `"${padding}"`,
    ]);
});

test("only a formula character after invisible ones is neutralised", () => {
    const lines = csvDocument(["a"], [[" \u00ad+x"], ["x=1"], ["\u00a0@"]])
        .replace(/^\ufeff/, "")
        .split("\r\n");
    expect(lines.slice(1, 4)).toEqual([`"' \u00ad+x"`, '"x=1"', `"'\u00a0@"`]);
});

test("the production file names its columns and sources in the reader's language, one row per recorded interval in plant time", () => {
    const file = productionExport(
        plant,
        "2026-11-10",
        [
            interval("2026-11-10T05:00:00Z", "2026-11-10T05:30:00Z", 0.5),
            interval("2026-11-10T05:30:00Z", "2026-11-10T06:00:00Z", null),
            interval(
                "2026-11-10T06:00:00Z",
                "2026-11-10T06:30:00Z",
                0.25,
                "settlement_injection",
            ),
            interval(
                "2026-11-10T06:30:00Z",
                "2026-11-10T07:00:00Z",
                0.125,
                "realtime_generation",
            ),
        ],
        "actual-production",
    );
    expect(file.filename).toBe(
        "pyracast-dogu-array-2026-11-10-actual-production.csv",
    );
    expect(lines(file)).toEqual([
        '"Start","End","Actual (MWh)","Status","Source"',
        '"2026-11-10T08:00:00+03:00","2026-11-10T08:30:00+03:00",0.5,"Normal","Entered"',
        '"2026-11-10T09:00:00+03:00","2026-11-10T09:30:00+03:00",0.25,"Normal","UEVM"',
        '"2026-11-10T09:30:00+03:00","2026-11-10T10:00:00+03:00",0.125,"Normal","Real-time (EPİAŞ)"',
        "",
    ]);
    expect(lines(file, "tr")).toEqual([
        '"Başlangıç","Bitiş","Gerçekleşen (MWh)","Durum","Kaynak"',
        '"2026-11-10T08:00:00+03:00","2026-11-10T08:30:00+03:00",0.5,"Normal","Girilen"',
        '"2026-11-10T09:00:00+03:00","2026-11-10T09:30:00+03:00",0.25,"Normal","UEVM"',
        '"2026-11-10T09:30:00+03:00","2026-11-10T10:00:00+03:00",0.125,"Normal","Gerçek zamanlı (EPİAŞ)"',
        "",
    ]);
    expect(written(file).startsWith("\uFEFF")).toBe(true);
});

for (const [language, outageWord, source] of [
    ["en", "Outage", "Entered"],
    ["tr", "Kesinti", "Girilen"],
] as const)
    test(`a downloaded production file in ${language} is imported again after a value and a status are corrected`, () => {
        const file = productionExport(
            plant,
            "2026-11-10",
            [
                interval("2026-11-10T05:00:00Z", "2026-11-10T05:30:00Z", 0.5),
                interval("2026-11-10T05:30:00Z", "2026-11-10T06:00:00Z", 0.25),
            ],
            "actual-production",
        );
        const [header, first, second] = written(file, language).split("\r\n");
        const corrected = [
            header,
            first,
            second!
                .replace(",0.25,", ",0.375,")
                .replace('"Normal"', `"${outageWord}"`),
        ].join("\r\n");
        for (const reader of ["en", "tr"] as const) {
            const parsed = parseProduction(corrected, ...csvTexts(reader));
            expect(parsed.unit).toBe("MWh");
            expect(parsed.observations).toEqual([
                {
                    starts_at: "2026-11-10T08:00:00+03:00",
                    ends_at: "2026-11-10T08:30:00+03:00",
                    energy: 0.5,
                    quality: "valid",
                    source,
                },
                {
                    starts_at: "2026-11-10T08:30:00+03:00",
                    ends_at: "2026-11-10T09:00:00+03:00",
                    energy: 0.375,
                    quality: "outage",
                    source,
                },
            ]);
        }
    });

const half = (start: string, end: string) =>
    `2026-01-01T${start}:00+03:00,2026-01-01T${end}:00+03:00`;

test("the production import reads the column names of either language and of earlier files, with the unit in the energy column", () => {
    for (const [header, row, unit] of [
        [
            "Start,End,Actual (kWh),Status,Source",
            `${half("09:00", "09:30")},250,Production curtailed,Meter`,
            "kWh",
        ],
        [
            "Başlangıç,Bitiş,Gerçekleşen (MWh),Durum,Kaynak",
            `${half("09:00", "09:30")},0.25,Üretim kısıldı,Meter`,
            "MWh",
        ],
        [
            "Kaynak,Status,Gerçekleşen (kWh),End,starts_at",
            "Meter,curtailed,250,2026-01-01T09:30:00+03:00,2026-01-01T09:00:00+03:00",
            "kWh",
        ],
    ] as const) {
        const parsed = parseProduction(`${header}\n${row}`, ...csvTexts());
        expect(parsed.unit).toBe(unit);
        expect(parsed.observations).toEqual([
            {
                starts_at: "2026-01-01T09:00:00+03:00",
                ends_at: "2026-01-01T09:30:00+03:00",
                energy: unit === "kWh" ? 250 : 0.25,
                quality: "curtailed",
                source: "Meter",
            },
        ]);
    }
});

test("the production import refuses a file with another column and names its columns as the file has them in its messages", () => {
    expect(() =>
        parseProduction(
            `Plant,Start,End,Actual (MWh),Status,Source\nA,${half("09:00", "09:30")},1,Normal,Meter`,
            ...csvTexts(),
        ),
    ).toThrow(
        "Use the columns of a file downloaded under Actual production: Start, End, Actual (MWh), Status and Source.",
    );
    const turkish = "Başlangıç,Bitiş,Gerçekleşen (MWh),Durum,Kaynak\n";
    expect(() =>
        parseProduction(
            `${turkish}${half("09:00", "09:30")},-1,Normal,Sayaç`,
            ...csvTexts("tr"),
        ),
    ).toThrow(
        "Satır 2: Gerçekleşen (MWh) sütununa sıfır veya pozitif bir sayı yazın.",
    );
    expect(() =>
        parseProduction(
            `${turkish}2026-01-01T09:00:00,2026-01-01T09:30:00+03:00,1,Normal,Sayaç`,
            ...csvTexts("tr"),
        ),
    ).toThrow(
        "Satır 2: Başlangıç ve Bitiş sütunlarına saatleri örnekteki gibi saat farkıyla, aralarında 30 veya 60 dakika olacak şekilde yazın.",
    );
    expect(() =>
        parseProduction(
            `${turkish}${half("09:00", "09:30")},1,Bilinmiyor,Sayaç`,
            ...csvTexts("tr"),
        ),
    ).toThrow(
        "Satır 2: Durum sütununa Normal, Kesinti, Üretim kısıldı veya Geçersiz değer; Kaynak sütununa en fazla 200 karakter yazın.",
    );
    expect(() =>
        parseProduction(
            `starts_at,ends_at,energy_mwh,quality,source\n${half("09:00", "09:30")},1,Normal,`,
            ...csvTexts(),
        ),
    ).toThrow(
        "Row 2: write Normal, Outage, Production curtailed or Invalid value in the quality column and up to 200 characters in the source column.",
    );
});

test("an overlap names the two rows of the file that cover the same time", () => {
    const file = [
        "Start,End,Actual (MWh),Status,Source",
        `${half("10:00", "10:30")},1,Normal,Meter`,
        `${half("09:00", "09:30")},1,Normal,Meter`,
        `${half("10:00", "10:30")},2,Normal,Meter`,
    ].join("\n");
    expect(() => parseProduction(file, ...csvTexts())).toThrow(
        "Rows 2 and 4 cover the same time. Remove one of them or correct its time.",
    );
    expect(() => parseProduction(file, ...csvTexts("tr"))).toThrow(
        "Satır 2 ile satır 4 aynı saati kapsıyor. Birini kaldırın veya saatini düzeltin.",
    );
});

test("an hour of half hours with different statuses is exported as such and refused on import with its way out", () => {
    const row = interval("2026-11-10T05:00:00Z", "2026-11-10T06:00:00Z", 0.75);
    const file = productionExport(
        plant,
        "2026-11-10",
        [
            {
                ...row,
                effective: {
                    energy_mwh: 0.75,
                    quality: "mixed",
                    source: "multiple_manual",
                    source_kind: "manual",
                    provisional: false,
                    aggregated: true,
                },
            },
        ],
        "actual-production",
    );
    expect(lines(file)[1]).toBe(
        '"2026-11-10T08:00:00+03:00","2026-11-10T09:00:00+03:00",0.75,"Different statuses","Entered"',
    );
    expect(lines(file, "tr")[1]).toBe(
        '"2026-11-10T08:00:00+03:00","2026-11-10T09:00:00+03:00",0.75,"Farklı durumlar","Girilen"',
    );
    expect(() => parseProduction(written(file), ...csvTexts())).toThrow(
        "Row 2: the half hours of this hour have different statuses. Correct each half hour separately.",
    );
    expect(() =>
        parseProduction(written(file, "tr"), ...csvTexts("tr")),
    ).toThrow(
        "Satır 2: bu saatin yarım saatlerinin durumu farklı. Her yarım saati ayrı düzeltin.",
    );
});

test("an empty list gives the header and no fabricated row", () => {
    for (const file of [
        productionExport(plant, "2026-11-10", [], "actual-production"),
        outagesExport(plant, [], "outages"),
        calibrationsExport(plant, [], "calibrations"),
    ])
        expect(lines(file)).toHaveLength(2);
});

test("the outage file names its columns, types and states as the screen does and keeps typed text inert", () => {
    const file = outagesExport(plant, [outage], "outages");
    expect(file.filename).toBe("pyracast-dogu-array-outages.csv");
    expect(lines(file).slice(0, 2)).toEqual([
        '"Plant","Title","Type","Status","Start","End","Panel capacity still in service (MW)","Inverter capacity still in service (MW)","Power that can still be exported (MW)","Notes"',
        `"Doğu: array","'=HYPERLINK(""x""), ""quoted""","Inverter","Ongoing","2026-11-11T11:00:00+03:00",,0,,1.5,"line one\nline two"`,
    ]);
    expect(lines(file, "tr").slice(0, 2)).toEqual([
        '"Santral","Başlık","Tür","Durum","Başlangıç","Bitiş","Devrede kalan panel gücü (MW)","Devrede kalan inverter gücü (MW)","Şebekeye verilebilecek güç (MW)","Notlar"',
        `"Doğu: array","'=HYPERLINK(""x""), ""quoted""","İnverter","Devam ediyor","2026-11-11T11:00:00+03:00",,0,,1.5,"line one\nline two"`,
    ]);
});

const result = {
    provisional_production: false,
    training_days: 20,
    holdout_days: 7,
    valid_days: 27,
    baseline: { daily_mae_mwh: 2, interval_mae_mw: 0.4 },
    candidate: { daily_mae_mwh: 1.5, interval_mae_mw: 0.3 },
};

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
    result,
    dataset_sha256: "f00",
    activation_blocker: "configuration_changed",
    dataset_manifest: { provider: "secret" },
};

test("the calibration list file says which calibration is in use and preliminary as the screen does", () => {
    const inUse: Schemas["CalibrationOutput"] = {
        ...run,
        selection_state: "active",
        assessment: "provisional",
        result: { ...result, provisional_production: true },
    };
    const list = calibrationsExport(plant, [inUse, run], "calibrations");
    expect(list.filename).toBe("pyracast-dogu-array-calibrations.csv");
    expect(lines(list).slice(0, 3)).toEqual([
        '"Plant","No.","Period start","Period end","Status","In use","Preliminary","Real-time (EPİAŞ)","Factor"',
        '"Doğu: array",40,"2026-01-01","2026-01-30","Calculated","Yes","Yes","Yes",1.1',
        '"Doğu: array",40,"2026-01-01","2026-01-30","Calculated","No","No","No",1.1',
    ]);
    expect(lines(list, "tr").slice(0, 3)).toEqual([
        '"Santral","No.","Dönem başlangıcı","Dönem bitişi","Durum","Kullanımda","Ön sonuç","Gerçek zamanlı (EPİAŞ)","Katsayı"',
        '"Doğu: array",40,"2026-01-01","2026-01-30","Hesaplandı","Evet","Evet","Evet",1.1',
        '"Doğu: array",40,"2026-01-01","2026-01-30","Hesaplandı","Hayır","Hayır","Hayır",1.1',
    ]);
});

test("the calibration file holds the detail's days and daily errors and no calculation record", () => {
    const detail = calibrationExport(plant, run, "calibration-40");
    expect(detail.filename).toBe("pyracast-dogu-array-calibration-40.csv");
    expect(lines(detail)).toEqual([
        '"Plant","No.","Period start","Period end","Status","In use","Preliminary","Real-time (EPİAŞ)","Factor","Calibration days","Test days","Error without calibration (MWh)","Error with this calibration (MWh)"',
        '"Doğu: array",40,"2026-01-01","2026-01-30","Calculated","No","No","No",1.1,20,7,2,1.5',
        "",
    ]);
    expect(lines(detail, "tr").slice(0, 2)).toEqual([
        '"Santral","No.","Dönem başlangıcı","Dönem bitişi","Durum","Kullanımda","Ön sonuç","Gerçek zamanlı (EPİAŞ)","Katsayı","Kalibrasyon günleri","Test günleri","Kalibrasyonsuz hata (MWh)","Bu kalibrasyonla hata (MWh)"',
        '"Doğu: array",40,"2026-01-01","2026-01-30","Hesaplandı","Hayır","Hayır","Hayır",1.1,20,7,2,1.5',
    ]);
});

const comparisonDay = {
    date: "2026-11-01",
    status: "matched",
    reason: null,
    source_kind: "estimated_actuals",
    measured_energy_mwh: 10,
    predicted_energy_mwh: 11,
    error_mwh: 1,
    provisional_production: true,
} as const;

const notCompared = [
    {
        ...comparisonDay,
        date: "2026-11-02",
        status: "missing",
        reason: "forecast_not_available",
        source_kind: null,
        predicted_energy_mwh: null,
        error_mwh: null,
        provisional_production: false,
    },
    {
        ...comparisonDay,
        date: "2026-11-03",
        status: "missing",
        reason: null,
        source_kind: "forecast",
        measured_energy_mwh: null,
        predicted_energy_mwh: 4,
        error_mwh: null,
        provisional_production: false,
    },
] as const;

test("the comparison file names each day's status and the reason a day was not compared", () => {
    const file = performanceExport(
        plant,
        {
            start_date: "2026-11-01",
            end_date: "2026-11-03",
            comparison: "archived_forecast",
            days: [comparisonDay, ...notCompared],
        },
        {
            file: "comparison",
            comparison: "Forecast accuracy",
            forecast: "Forecast",
            satellite: "Production calculated from satellite data",
        },
    );
    expect(file.filename).toBe(
        "pyracast-dogu-array-2026-11-01-2026-11-03-comparison.csv",
    );
    expect(lines(file)).toEqual([
        '"Plant","Comparison","Date","Status","Reason","Data","Actual (MWh)","Forecast (MWh)","Forecast − actual (MWh)","Real-time (EPİAŞ)"',
        '"Doğu: array","Forecast accuracy","2026-11-01","Compared",,"Production calculated from satellite data",10,11,1,"Yes"',
        '"Doğu: array","Forecast accuracy","2026-11-02","Not compared","No forecast to compare for this day",,10,,,"No"',
        '"Doğu: array","Forecast accuracy","2026-11-03","Not compared","Data is missing for this day","Forecast",,4,,"No"',
        "",
    ]);
});

test("the comparison file of expected production uses the expected columns in either language", () => {
    const file = (
        names: Record<"file" | "comparison" | "forecast" | "satellite", string>,
    ) =>
        performanceExport(
            plant,
            {
                start_date: "2026-11-01",
                end_date: "2026-11-01",
                comparison: "historical_model",
                days: [comparisonDay],
            },
            names,
        );
    expect(
        lines(
            file({
                file: "comparison",
                comparison: "Weather-based expected production",
                forecast: "Forecast",
                satellite: "Production calculated from satellite data",
            }),
        ).slice(0, 2),
    ).toEqual([
        '"Plant","Comparison","Date","Status","Reason","Data","Actual (MWh)","Expected (MWh)","Expected − actual (MWh)","Real-time (EPİAŞ)"',
        '"Doğu: array","Weather-based expected production","2026-11-01","Compared",,"Production calculated from satellite data",10,11,1,"Yes"',
    ]);
    expect(
        lines(
            file({
                file: "karsilastirma",
                comparison: "Hava koşullarına göre beklenen üretim",
                forecast: "Tahmin",
                satellite: "Uydu verisinden hesaplanan üretim",
            }),
            "tr",
        ).slice(0, 2),
    ).toEqual([
        '"Santral","Karşılaştırma","Tarih","Durum","Neden","Veri","Gerçekleşen (MWh)","Beklenen (MWh)","Beklenen − gerçekleşen (MWh)","Gerçek zamanlı (EPİAŞ)"',
        '"Doğu: array","Hava koşullarına göre beklenen üretim","2026-11-01","Karşılaştırıldı",,"Uydu verisinden hesaplanan üretim",10,11,1,"Evet"',
    ]);
});

test("every plant file keeps its dates and kind when the plant name is long or has no ASCII letter", () => {
    const long = { ...plant, name: "Ç".repeat(200) };
    const names = [
        productionExport(long, "2026-11-10", [], "gerceklesen-uretim"),
        performanceExport(
            long,
            {
                start_date: "2026-11-01",
                end_date: "2026-11-02",
                comparison: "archived_forecast",
                days: [],
            },
            {
                file: "karsilastirma",
                comparison: "",
                forecast: "",
                satellite: "",
            },
        ),
        calibrationsExport(long, [], "kalibrasyonlar"),
        calibrationExport(long, run, "kalibrasyon-40"),
        outagesExport(long, [], "kesintiler"),
        plantExport(
            long,
            { latitude: 0, longitude: 0 },
            null,
            "teknik-bilgiler",
        ),
    ].map((file) => file.filename);
    expect(names.map((name) => name.length)).toEqual([
        154, 154, 154, 154, 154, 154,
    ]);
    expect(names.map((name) => name.replace(/^pyracast-c+-/, ""))).toEqual([
        "2026-11-10-gerceklesen-uretim.csv",
        "2026-11-01-2026-11-02-karsilastirma.csv",
        "kalibrasyonlar.csv",
        "kalibrasyon-40.csv",
        "kesintiler.csv",
        "teknik-bilgiler.csv",
    ]);
    expect(
        productionExport(
            { ...plant, name: "東京" },
            "2026-11-10",
            [],
            "actual-production",
        ).filename,
    ).toBe("pyracast-plant-2026-11-10-actual-production.csv");
});

const forecastRow = {
    start: "2026-11-12T08:00:00Z",
    end: "2026-11-12T09:00:00Z",
    mean_power_mw: 2,
    energy_mwh: 2,
    ghi_wm2: 410,
    quality_flags: ["clipped", "estimated"],
    affected_mean_power_mw: 0,
    affected_energy_mwh: 0,
    affected_quality_flags: [],
};

test("the generation file names its columns as the values table does, without quality flags", () => {
    const names = {
        file: "generation",
        forecast: "Forecast",
        satellite: "Production calculated from satellite data",
    };
    const forecast = { date: "2026-11-12", source_kind: "forecast" };
    const file = forecastExport(plant, forecast, [forecastRow], {
        electrical: true,
        scenario: false,
        names,
    });
    expect(file.filename).toBe("pyracast-dogu-array-2026-11-12-generation.csv");
    expect(lines(file).slice(0, 2)).toEqual([
        '"Plant","Data","Start","End","Forecast (MWh)","Average power (MW)"',
        '"Doğu: array","Forecast","2026-11-12T11:00:00+03:00","2026-11-12T12:00:00+03:00",2,2',
    ]);
    const turkish = forecastExport(plant, forecast, [forecastRow], {
        electrical: true,
        scenario: false,
        names: { ...names, file: "uretim", forecast: "Tahmin" },
    });
    expect(turkish.filename).toBe("pyracast-dogu-array-2026-11-12-uretim.csv");
    expect(lines(turkish, "tr").slice(0, 2)).toEqual([
        '"Santral","Veri","Başlangıç","Bitiş","Tahmin (MWh)","Ortalama güç (MW)"',
        '"Doğu: array","Tahmin","2026-11-12T11:00:00+03:00","2026-11-12T12:00:00+03:00",2,2',
    ]);
});

test("the generation file with an outage holds both figures with and without it", () => {
    const file = forecastExport(
        plant,
        { date: "2026-11-12", source_kind: "forecast" },
        [forecastRow],
        {
            electrical: true,
            scenario: true,
            names: { file: "generation", forecast: "Forecast", satellite: "" },
        },
    );
    expect(lines(file).slice(0, 2)).toEqual([
        '"Plant","Data","Start","End","Without outage (MWh)","With outage (MWh)","Average power without outage (MW)","Average power with outage (MW)"',
        '"Doğu: array","Forecast","2026-11-12T11:00:00+03:00","2026-11-12T12:00:00+03:00",2,0,2,0',
    ]);
    expect(lines(file, "tr")[0]).toBe(
        '"Santral","Veri","Başlangıç","Bitiş","Kesinti olmadan (MWh)","Kesinti dahil (MWh)","Kesinti olmadan ortalama güç (MW)","Kesinti dahil ortalama güç (MW)"',
    );
});

test("the irradiance file holds irradiance in W/m² in either language", () => {
    const file = forecastExport(
        plant,
        { date: "2026-11-12", source_kind: "forecast" },
        [forecastRow],
        {
            electrical: false,
            scenario: false,
            names: { file: "isinim", forecast: "Tahmin", satellite: "" },
        },
    );
    expect(file.filename).toBe("pyracast-dogu-array-2026-11-12-isinim.csv");
    expect(lines(file, "tr").slice(0, 2)).toEqual([
        '"Santral","Veri","Başlangıç","Bitiş","Işınım (W/m²)"',
        '"Doğu: array","Tahmin","2026-11-12T11:00:00+03:00","2026-11-12T12:00:00+03:00",410',
    ]);
    expect(lines(file)[0]).toBe(
        '"Plant","Data","Start","End","Irradiance (W/m²)"',
    );
});

test("the generation file names satellite rows and writes an interval of both kinds as forecast", () => {
    const names = {
        file: "generation",
        forecast: "Forecast",
        satellite: "Production calculated from satellite data",
    };
    const row = (kinds: string[]) => ({
        start: "2026-11-12T08:00:00Z",
        end: "2026-11-12T09:00:00Z",
        energy_mwh: 2,
        source_kinds: kinds,
    });
    const satellite = forecastExport(
        { ...plant, name: "IŞIK Çiftliği" },
        { date: "2026-11-11", source_kind: "estimated_actuals" },
        [row(["estimated_actuals"])],
        { electrical: true, scenario: false, names },
    );
    expect(satellite.filename).toBe(
        "pyracast-isik-ciftligi-2026-11-11-generation.csv",
    );
    expect(lines(satellite).slice(0, 2)).toEqual([
        '"Plant","Data","Start","End","Energy (MWh)","Average power (MW)"',
        '"IŞIK Çiftliği","Production calculated from satellite data","2026-11-12T11:00:00+03:00","2026-11-12T12:00:00+03:00",2,',
    ]);
    const mixed = forecastExport(
        plant,
        { date: "2026-11-12", source_kind: "mixed" },
        [
            row(["estimated_actuals"]),
            row(["estimated_actuals", "forecast"]),
            row(["forecast"]),
        ],
        { electrical: true, scenario: false, names },
    );
    expect(
        lines(mixed)
            .slice(0, 4)
            .map((line) => line.split(",")[1]),
    ).toEqual([
        '"Data"',
        '"Production calculated from satellite data"',
        '"Forecast"',
        '"Forecast"',
    ]);
    expect(lines(mixed)[0]).toBe(
        '"Plant","Data","Start","End","Forecast (MWh)","Average power (MW)"',
    );
});

test("forecast file names keep the date and kind for the longest plant name and name a plant without ASCII letters", () => {
    const longest = "Güneş ".repeat(34).slice(0, 200);
    const file = (name: string, kind: string) =>
        forecastExport(
            { ...plant, name },
            { date: "2026-11-14", source_kind: "forecast" },
            [],
            {
                electrical: kind === "generation",
                scenario: false,
                names: { file: kind, forecast: "Forecast", satellite: "" },
            },
        ).filename;
    const generation = file(longest, "generation");
    const irradiance = file(longest, "irradiance");
    expect(generation).not.toBe(irradiance);
    expect(generation).toMatch(
        /^pyracast-gunes[a-z-]*[a-z]-2026-11-14-generation\.csv$/,
    );
    expect(irradiance).toMatch(
        /^pyracast-gunes[a-z-]*[a-z]-2026-11-14-irradiance\.csv$/,
    );
    expect(generation).toHaveLength(154);
    expect(file("東京", "generation")).toBe(
        "pyracast-plant-2026-11-14-generation.csv",
    );
});

test("the technical details file names the location and technical details as the screens do", () => {
    const file = plantExport(
        plant,
        { latitude: 36.9, longitude: 30.7 },
        {
            dc_capacity_mw: 5,
            inverter_capacity_mw: 4,
            export_limit_mw: null,
            measurement_basis: "net_export",
            mounting_type: "fixed",
            surface_tilt_degrees: 25,
            surface_azimuth_degrees: 180,
        },
        "technical-details",
    );
    expect(file.filename).toBe("pyracast-dogu-array-technical-details.csv");
    expect(lines(file).slice(0, 2)).toEqual([
        '"Plant","Latitude (°)","Longitude (°)","Time zone","Panel capacity (MWp)","Inverter capacity (MW AC)","Grid export limit (MW)","Meter point","Mounting","Panel tilt (°)","Panel azimuth (°)"',
        '"Doğu: array",36.9,30.7,"Europe/Istanbul",5,4,,"Energy delivered to the grid","Fixed",25,180',
    ]);
    expect(lines(file, "tr").slice(0, 2)).toEqual([
        '"Santral","Enlem (°)","Boylam (°)","Saat dilimi","Panel kurulu gücü (MWp)","İnverter gücü (MW AC)","Şebekeye veriş sınırı (MW)","Ölçüm noktası","Montaj","Panel eğimi (°)","Panel azimutu (°)"',
        '"Doğu: array",36.9,30.7,"Europe/Istanbul",5,4,,"Şebekeye verilen enerji","Sabit",25,180',
    ]);
});

test("a plant whose technical details are not saved yet leaves them empty", () => {
    const file = plantExport(
        plant,
        { latitude: 36.9, longitude: 30.7 },
        null,
        "technical-details",
    );
    expect(lines(file)[1]).toBe(
        '"Doğu: array",36.9,30.7,"Europe/Istanbul",,,,,,,',
    );
});
