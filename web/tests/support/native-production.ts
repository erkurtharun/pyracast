import type { components } from "../../src/lib/schema";

type Interval = components["schemas"]["ProductionIntervalOutput"];

export function nativeProductionHistory(
    date: string,
    timezone: string,
    rows: (Pick<Interval, "starts_at" | "ends_at"> & Partial<Interval>)[],
): components["schemas"]["ProductionHistoryOutput"] {
    const intervals: Interval[] = rows.map((row) => ({
        reference_available: true,
        observation: null,
        can_enter: true,
        unavailable_reason: null,
        revision_id: null,
        effective: null,
        provider_original: null,
        provider_originals: [],
        can_edit: true,
        can_revert: false,
        edit_unavailable_reason: null,
        ...row,
    }));
    const observations = intervals.flatMap((row) =>
        row.observation ? [row.observation] : [],
    );
    return {
        plant_id: 21,
        configuration_id: 31,
        timezone,
        start_date: date,
        end_date: date,
        available_start_date: date,
        available_end_date: date,
        resolution_minutes: 60,
        native_intervals: true,
        slot_scope: "full_day",
        daylight_policy: "reference_irradiance",
        days: [
            {
                date,
                available_count: intervals.length,
                recorded_count: observations.length,
                missing_count: intervals.filter((row) => row.effective === null)
                    .length,
                sunrise: `${date}T06:00:00Z`,
                sunset: `${date}T16:00:00Z`,
                daylight_status: "normal",
                daylight_windows: [
                    {
                        starts_at: `${date}T06:00:00Z`,
                        ends_at: `${date}T16:00:00Z`,
                    },
                ],
                total_mwh: intervals.some((row) => row.effective === null)
                    ? null
                    : intervals.reduce(
                          (total, row) => total + row.effective!.energy_mwh,
                          0,
                      ),
                complete: intervals.every((row) => row.effective !== null),
            },
        ],
        intervals,
        observations,
        revisions: observations,
    };
}
