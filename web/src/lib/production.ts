import Papa from "papaparse";
import { createTranslator } from "next-intl";
import { isLanguage } from "@/i18n/language";
import { DisplayError } from "@/lib/api/errors";
import { messages } from "@/i18n/messages";
export const QUALITIES = ["valid", "outage", "curtailed", "invalid"] as const;
export type Quality = (typeof QUALITIES)[number];
export function qualityOf(value: string | null | undefined): Quality {
    if (value === null || value === undefined) return "valid";
    const selected = QUALITIES.find((quality) => quality === value);
    if (selected === undefined)
        throw new Error("Unrecognized production quality.");
    return selected;
}
export type Observation = {
    starts_at: string;
    ends_at: string;
    energy: number;
    quality: Quality;
    source: string;
};
export function parseProduction(text: string, language = "en"): Observation[] {
    const locale = isLanguage(language) ? language : "en";
    const t = createTranslator({
        locale,
        messages: messages[locale].ProductionCsv,
    });
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024)
        throw new DisplayError(t("size"));
    const parsed = Papa.parse<Record<string, string>>(text, {
        header: true,
        delimiter: ",",
        dynamicTyping: false,
        skipEmptyLines: "greedy",
        transformHeader: (value) => value.trim(),
        transform: (value) => value.trim(),
    });
    if (
        parsed.meta.fields?.join(",") !==
        "starts_at,ends_at,energy,quality,source"
    )
        throw new DisplayError(t("header"));
    const [error] = parsed.errors;
    if (error) throw new DisplayError(t("parse", { reason: error.message }));
    if (parsed.data.length < 1 || parsed.data.length > 2000)
        throw new DisplayError(t("count"));
    const rows = parsed.data.map((row, index) => {
        const {
            starts_at = "",
            ends_at = "",
            energy: rawEnergy,
            quality,
            source,
        } = row;
        const start = Date.parse(starts_at),
            end = Date.parse(ends_at),
            energy = Number(rawEnergy);
        const aware = /(?:Z|[+-]\d{2}:\d{2})$/;
        if (
            !aware.test(starts_at) ||
            !aware.test(ends_at) ||
            !Number.isFinite(start) ||
            !Number.isFinite(end) ||
            ![1800000, 3600000].includes(end - start)
        )
            throw new DisplayError(t("interval", { row: index + 2 }));
        if (end > Date.now())
            throw new DisplayError(t("future", { row: index + 2 }));
        if (!rawEnergy || !Number.isFinite(energy) || energy < 0)
            throw new DisplayError(t("energy", { row: index + 2 }));
        const known = QUALITIES.find((value) => value === quality);
        if (!known || !source || source.length > 200)
            throw new DisplayError(t("quality", { row: index + 2 }));
        return { starts_at, ends_at, energy, quality: known, source };
    });
    const sorted = [...rows].sort(
        (a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at),
    );
    if (
        sorted.some((row, index) => {
            const previous = sorted[index - 1];
            return (
                previous !== undefined &&
                Date.parse(row.starts_at) < Date.parse(previous.ends_at)
            );
        })
    )
        throw new DisplayError(t("overlap"));
    return rows;
}
