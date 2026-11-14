export const languages = ["en", "tr"] as const;
export type Language = (typeof languages)[number];

export function isLanguage(value: unknown): value is Language {
    return value === "en" || value === "tr";
}
