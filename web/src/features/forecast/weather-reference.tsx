"use client";
import { useTranslations } from "next-intl";
import { Stat } from "@/ui/ui";

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
    const t = useTranslations("WeatherReference");

    return (
        <section className="panel" aria-labelledby="weather-reference-title">
            <h3 id="weather-reference-title">
                {t("independent_weather_model")}
            </h3>
            <p className="fine">
                {t("an_independent_solar_irradiation_forecast_separate_from")}
            </p>
            {reference.status === "unavailable" ? (
                <p>{t("weather_model_data_is_currently_unavailable")}</p>
            ) : (
                <>
                    {reference.status === "partial" && (
                        <p>{t("the_daily_total_is_unavailable_the_partial")}</p>
                    )}
                    <div className="stats">
                        <Stat
                            label={t("daily_solar_irradiation")}
                            value={reference.energy_kwh_m2}
                            unit="kWh/m²"
                        />
                        {reference.status === "partial" && (
                            <Stat
                                label={t("partial_irradiation")}
                                value={reference.partial_energy_kwh_m2}
                                unit="kWh/m²"
                            />
                        )}
                        <Stat
                            label={t("coverage")}
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
                {t("weather_attribution")}{" "}
                <a href="https://open-meteo.com/">Open-Meteo.com</a> ·{" "}
                <a href="https://creativecommons.org/licenses/by/4.0/">
                    CC BY 4.0
                </a>
                .{" "}
                {reference.attribution?.changes ===
                "Intervals normalized; daily energy calculated by Pyracast."
                    ? t("normalized_attribution")
                    : reference.attribution?.changes}
            </p>
        </section>
    );
}
