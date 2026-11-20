"use client";
import { useLocale, useTranslations } from "next-intl";
import { providerOriginals, type Interval } from "./model";

export function ProductionReading({
    row,
    timeFormatter,
}: {
    row: Interval;
    timeFormatter: Intl.DateTimeFormat;
}) {
    const t = useTranslations("ProductionHistory");
    const locale = useLocale();
    const number = (value: number) =>
        new Intl.NumberFormat(locale, { maximumFractionDigits: 3 }).format(
            value,
        );
    const time = (value: string) => timeFormatter.format(new Date(value));
    const corrected =
        row.observation != null || row.effective?.aggregated === true;
    const originals = providerOriginals(row);
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
                    : number(row.effective.energy_mwh)}{" "}
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
                    {t.has(quality) ? t(quality) : quality}
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
                                    Date.parse(original.starts_at) !==
                                        Date.parse(row.starts_at) ||
                                    Date.parse(original.ends_at) !==
                                        Date.parse(row.ends_at)
                                    ? "provider_original_interval"
                                    : "provider_original",
                                {
                                    interval: `${time(original.starts_at)}–${time(original.ends_at)}`,
                                    value: `${number(original.energy_mwh)} MWh`,
                                },
                            )}
                        </div>
                    ))
                ) : (
                    <div className="production-original">
                        {t("provider_original", { value: t("missing") })}
                    </div>
                ))}
        </>
    );
}
