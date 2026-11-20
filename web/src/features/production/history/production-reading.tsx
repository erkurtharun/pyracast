"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { qualityOf } from "@/lib/production";
import { useLabel } from "@/ui/vocabulary";
import { sameInterval, type Interval } from "./model";

export function ProductionReading({
    row,
    timeFormatter,
}: {
    row: Interval;
    timeFormatter: Intl.DateTimeFormat;
}) {
    const t = useTranslations("ProductionHistory");
    const format = useFormat();
    const label = useLabel();
    const time = (value: string) => timeFormatter.format(new Date(value));
    const corrected =
        row.observation != null || row.effective?.aggregated === true;
    const originals = row.provider_originals;
    const source =
        row.effective?.source_kind === "settlement_injection"
            ? "settlement"
            : row.effective?.source_kind === "realtime_generation"
              ? "provisional"
              : "manual";
    const quality = row.effective?.quality;
    return (
        <>
            <div className="production-value">
                {row.effective == null
                    ? t("missing")
                    : format.number(row.effective.energy_mwh)}{" "}
                {corrected ? (
                    <span className="production-correction">
                        {t("corrected")}
                    </span>
                ) : (
                    row.effective && <span className="fine">{t(source)}</span>
                )}
            </div>
            {quality && quality !== "valid" && (
                <div className="fine">
                    {label("ObservationQuality", qualityOf(quality))}
                </div>
            )}
            {corrected &&
                (originals.length ? (
                    originals.map((original) => (
                        <div
                            className="production-original"
                            key={`${original.snapshot_id}/${original.starts_at}`}
                        >
                            {t(
                                originals.length > 1 ||
                                    !sameInterval(original, row)
                                    ? "provider_original_interval"
                                    : "provider_original",
                                {
                                    interval: `${time(original.starts_at)}–${time(original.ends_at)}`,
                                    value: format.quantity(
                                        original.energy_mwh,
                                        "MWh",
                                    ),
                                },
                            )}
                        </div>
                    ))
                ) : (
                    <div className="production-original">
                        {t("provider_original", { value: t("missing") })}
                    </div>
                ))}
            {row.previous_configuration_observations.map((observation) => (
                <div className="production-original" key={observation.id}>
                    {t(
                        sameInterval(observation, row)
                            ? "recorded_under"
                            : "recorded_under_interval",
                        {
                            revision: observation.configuration_revision,
                            interval: `${time(observation.starts_at)}–${time(observation.ends_at)}`,
                            value: format.quantity(
                                observation.energy_mwh,
                                "MWh",
                            ),
                        },
                    )}
                </div>
            ))}
        </>
    );
}
