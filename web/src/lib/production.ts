import Papa from "papaparse";
import { createTranslator } from "next-intl";
import { isLanguage } from "@/i18n/language";
import { messages } from "@/i18n/messages";
export type Observation = {
    starts_at: string;
    ends_at: string;
    energy: number;
    quality: string;
    source: string;
};
export function parseProduction(text: string, language = "en"): Observation[] {
    const locale = isLanguage(language) ? language : "en";
    const t = createTranslator({
        locale,
        messages: messages[locale].ProductionCsv,
    });
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024)
        throw new Error(t("size"));
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
        throw new Error(t("header"));
    if (parsed.errors.length)
        throw new Error(t("parse", { reason: parsed.errors[0].message }));
    if (parsed.data.length < 1 || parsed.data.length > 2000)
        throw new Error(t("count"));
    const rows = parsed.data.map((row, index) => {
        const { starts_at, ends_at, energy: rawEnergy, quality, source } = row;
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
            throw new Error(t("interval", { row: index + 2 }));
        if (end > Date.now()) throw new Error(t("future", { row: index + 2 }));
        if (!rawEnergy || !Number.isFinite(energy) || energy < 0)
            throw new Error(t("energy", { row: index + 2 }));
        if (
            !["valid", "outage", "curtailed", "invalid"].includes(quality) ||
            !source ||
            source.length > 200
        )
            throw new Error(t("quality", { row: index + 2 }));
        return { starts_at, ends_at, energy, quality, source };
    });
    const sorted = [...rows].sort(
        (a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at),
    );
    if (
        sorted.some(
            (row, index) =>
                index > 0 &&
                Date.parse(row.starts_at) <
                    Date.parse(sorted[index - 1].ends_at),
        )
    )
        throw new Error(t("overlap"));
    return rows;
}
