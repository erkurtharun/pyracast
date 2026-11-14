import { ScrollRegion } from "./scroll-region";

export type ForecastInterval = {
    start: string;
    end: string;
    ghi_wm2?: number | null;
    mean_power_mw?: number | null;
    energy_mwh?: number | null;
    quality_flags?: string[];
    source_snapshot_ids?: number[];
    affected_mean_power_mw?: number | null;
    affected_energy_mwh?: number | null;
    affected_quality_flags?: string[];
};

const numberFormatter = new Intl.NumberFormat("en", {
    maximumFractionDigits: 3,
});

function IntervalValue({
    value,
    power,
    electrical,
}: {
    value: number | null | undefined;
    power?: number | null;
    electrical: boolean;
}) {
    return (
        <>
            <span className="interval-value">
                {value == null ? "—" : numberFormatter.format(value)}
            </span>
            {value == null && (
                <span className="sr-only"> Data unavailable.</span>
            )}
            {electrical && (
                <span className="sr-only">
                    {power == null
                        ? " Average power unavailable."
                        : ` Average power: ${numberFormatter.format(power)} MW.`}
                </span>
            )}
        </>
    );
}

export function ForecastIntervals({
    rows,
    electrical,
    scenarioLabel,
    label,
    timezone,
    formatTime,
}: {
    rows: ForecastInterval[];
    electrical: boolean;
    scenarioLabel?: string;
    label: string;
    timezone: string;
    formatTime: (value: string) => string;
}) {
    const valueField = electrical ? "energy_mwh" : "ghi_wm2";
    const notes = rows.filter(
        (row) =>
            row.quality_flags?.length ||
            (scenarioLabel && row.affected_quality_flags?.length),
    );
    const missing = rows.some(
        (row) =>
            row[valueField] == null ||
            (scenarioLabel && row.affected_energy_mwh == null),
    );
    const intervalTime = (row: ForecastInterval) => (
        <>
            <time dateTime={row.start}>{formatTime(row.start)}</time>
            {"–"}
            <time dateTime={row.end}>{formatTime(row.end)}</time>
        </>
    );
    return (
        <details className="interval-details">
            <summary>View interval data ({rows.length} intervals)</summary>
            <ScrollRegion label={label}>
                <table className="forecast-interval-table">
                    <caption className="sr-only">
                        {label}, {timezone}
                    </caption>
                    <thead>
                        <tr>
                            <th scope="col">Time interval</th>
                            <th scope="col">
                                {electrical
                                    ? scenarioLabel
                                        ? "Normal capacity"
                                        : "Energy"
                                    : "Irradiance"}{" "}
                                <span className="interval-unit">
                                    ({electrical ? "MWh" : "W/m²"})
                                </span>
                            </th>
                            {scenarioLabel && (
                                <th scope="col">
                                    {scenarioLabel}{" "}
                                    <span className="interval-unit">(MWh)</span>
                                </th>
                            )}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.start}>
                                <td>{intervalTime(row)}</td>
                                <td>
                                    <IntervalValue
                                        value={row[valueField]}
                                        power={row.mean_power_mw}
                                        electrical={electrical}
                                    />
                                </td>
                                {scenarioLabel && (
                                    <td>
                                        <IntervalValue
                                            value={row.affected_energy_mwh}
                                            power={row.affected_mean_power_mw}
                                            electrical
                                        />
                                    </td>
                                )}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </ScrollRegion>
            {missing && <p className="fine">— Data unavailable; not zero.</p>}
            {notes.length > 0 && (
                <details className="interval-notes">
                    <summary>Interval notes ({notes.length})</summary>
                    <ul>
                        {notes.map((row) => (
                            <li key={row.start}>
                                <strong>{intervalTime(row)}</strong>
                                <div>
                                    {!!row.quality_flags?.length && (
                                        <p>
                                            {scenarioLabel &&
                                                "Normal capacity: "}
                                            {row.quality_flags
                                                .map((flag) =>
                                                    flag.replaceAll("_", " "),
                                                )
                                                .join(", ")}
                                        </p>
                                    )}
                                    {scenarioLabel &&
                                        !!row.affected_quality_flags
                                            ?.length && (
                                            <p>
                                                {scenarioLabel}:{" "}
                                                {row.affected_quality_flags
                                                    .map((flag) =>
                                                        flag.replaceAll(
                                                            "_",
                                                            " ",
                                                        ),
                                                    )
                                                    .join(", ")}
                                            </p>
                                        )}
                                </div>
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </details>
    );
}
