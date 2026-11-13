"use client";

export type DataSource =
    "auto" | "forecast" | "estimated_actuals" | "archived_forecast";

export function localDay(timezone: string, offset = 0): string {
    const today = new Date().toLocaleDateString("en-CA", {
        timeZone: timezone,
    });
    const date = new Date(`${today}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

export function DataSourceControl({
    value,
    onChange,
    allowArchivedForecast = false,
}: {
    value: DataSource;
    onChange: (source: DataSource) => void;
    allowArchivedForecast?: boolean;
}) {
    return (
        <>
            <label className="field">
                Data source
                <select
                    value={value}
                    onChange={(event) =>
                        onChange(event.target.value as DataSource)
                    }
                >
                    <option value="auto">Automatic</option>
                    <option value="forecast">
                        {allowArchivedForecast
                            ? "Forecast outlook"
                            : "Archived forecast"}
                    </option>
                    {allowArchivedForecast && (
                        <option value="archived_forecast">
                            Archived day-ahead forecast
                        </option>
                    )}
                    <option value="estimated_actuals">Estimated actuals</option>
                </select>
            </label>
            {value === "estimated_actuals" && (
                <span className="fine">
                    Completed local days only · model estimates, not meter
                    readings.
                </span>
            )}
            {value === "archived_forecast" && (
                <span className="fine">
                    Saved prior-day forecast under its original configuration.
                </span>
            )}
        </>
    );
}
