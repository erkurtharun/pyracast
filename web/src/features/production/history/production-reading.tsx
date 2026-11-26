"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { productionSource, qualityOf } from "@/lib/production";
import { useLabel } from "@/ui/vocabulary";
import { sameInterval, type Interval } from "./model";

export function ProductionReading({
    row,
    time,
}: {
    row: Interval;
    time: (value: string) => string;
}) {
    const t = useTranslations("ProductionHistory");
    const format = useFormat();
    const label = useLabel();
    const entered =
        row.observation != null || row.effective?.aggregated === true;
    const originals = row.provider_originals;
    const corrected = entered && originals.length > 0;
    const source = productionSource(row);
    const quality = row.effective?.quality;
    return (
        <>
            <div className="production-value">
                {row.effective == null
                    ? t("missing")
                    : format.recorded(row.effective.energy_mwh)}{" "}
                {row.effective && (
                    <span className="fine">
                        {source === "settlement" ? (
                            <abbr title={t("settlement_title")}>
                                {t("settlement")}
                            </abbr>
                        ) : (
                            t(source)
                        )}
                    </span>
                )}
                {corrected && (
                    <>
                        {" "}
                        <span className="production-correction">
                            {t("corrected")}
                        </span>
                    </>
                )}
            </div>
            {quality && quality !== "valid" && (
                <div className="fine">
                    {label("ObservationQuality", qualityOf(quality))}
                </div>
            )}
            {corrected &&
                originals.map((original) => (
                    <div
                        className="production-original"
                        key={`${original.snapshot_id}/${original.starts_at}`}
                    >
                        {t(
                            originals.length > 1 || !sameInterval(original, row)
                                ? "provider_original_interval"
                                : "provider_original",
                            {
                                interval: `${time(original.starts_at)}–${time(original.ends_at)}`,
                                value: format.recordedQuantity(
                                    original.energy_mwh,
                                    "MWh",
                                ),
                            },
                        )}
                    </div>
                ))}
            {row.previous_configuration_observations.map((observation) => (
                <div className="production-original" key={observation.id}>
                    {t(
                        sameInterval(observation, row)
                            ? "recorded_under"
                            : "recorded_under_interval",
                        {
                            interval: `${time(observation.starts_at)}–${time(observation.ends_at)}`,
                            value: format.recordedQuantity(
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
