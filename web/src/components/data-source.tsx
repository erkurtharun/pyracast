"use client";

export type DataSource = "auto" | "forecast" | "estimated_actuals";

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
}: {
    value: DataSource;
    onChange: (source: DataSource) => void;
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
                    <option value="forecast">Archived forecast</option>
                    <option value="estimated_actuals">Estimated actuals</option>
                </select>
            </label>
            {value === "estimated_actuals" && (
                <span className="fine">
                    Estimated actuals are available only for completed local
                    days.
                </span>
            )}
        </>
    );
}
