import { Stat } from "./ui";

type WeatherReference = {
    status: "ready" | "partial" | "unavailable";
    energy_kwh_m2?: number | null;
    partial_energy_kwh_m2?: number | null;
    coverage_fraction?: number;
    attribution: { changes: string };
};

export function WeatherReferenceCard({
    reference,
}: {
    reference: WeatherReference;
}) {
    return (
        <section className="panel" aria-labelledby="weather-reference-title">
            <h3 id="weather-reference-title">Independent weather model</h3>
            <p className="fine">
                An independent solar irradiation forecast, separate from the
                plant energy estimate.
            </p>
            {reference.status === "unavailable" ? (
                <p>Weather model data is currently unavailable.</p>
            ) : (
                <>
                    {reference.status === "partial" && (
                        <p>
                            The daily total is unavailable; the partial value
                            includes covered hours only.
                        </p>
                    )}
                    <div className="stats">
                        <Stat
                            label="Daily solar irradiation"
                            value={reference.energy_kwh_m2}
                            unit="kWh/m²"
                        />
                        {reference.status === "partial" && (
                            <Stat
                                label="Partial irradiation"
                                value={reference.partial_energy_kwh_m2}
                                unit="kWh/m²"
                            />
                        )}
                        <Stat
                            label="Coverage"
                            value={
                                typeof reference.coverage_fraction === "number"
                                    ? reference.coverage_fraction * 100
                                    : null
                            }
                            unit="%"
                        />
                    </div>
                </>
            )}
            <p className="fine">
                Weather data by{" "}
                <a href="https://open-meteo.com/">Open-Meteo.com</a> ·{" "}
                <a href="https://creativecommons.org/licenses/by/4.0/">
                    CC BY 4.0
                </a>
                . {reference.attribution?.changes}
            </p>
        </section>
    );
}
