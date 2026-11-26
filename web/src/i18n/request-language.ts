import { isLanguage, type Language } from "./language";

// The rule of pyracast.languages.negotiated_language: both answer the cases of web/tests/support/language-negotiation.json.
export const defaultLanguage: Language = "en";
export const languageCookie = "pyracast_language";
export const languageHeader = "x-pyracast-language";
const longestAcceptLanguage = 8192;
const languageRange =
    /^\s*([a-z]{1,8}(?:-[a-z0-9]{1,8})*|\*)\s*(?:;\s*q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\s*)?$/i;

export function languageCookieOptions(secure: boolean) {
    return {
        maxAge: 365 * 24 * 60 * 60,
        path: "/",
        httpOnly: true,
        sameSite: "lax",
        secure,
    } as const;
}

function browserLanguage(header: string): {
    language?: Language;
    rejected: Set<string>;
} {
    if (header.length > longestAcceptLanguage)
        return { rejected: new Set(["*"]) };
    let best: { language: Language; weight: number } | undefined;
    const rejected = new Set<string>();
    for (const item of header.split(",")) {
        const match = languageRange.exec(item);
        if (!match) continue;
        const [, tag = "", quality] = match;
        const base = tag.toLowerCase().replace(/-.*/, "");
        const weight = quality === undefined ? 1 : Number(quality);
        if (weight === 0) rejected.add(base);
        else if (isLanguage(base) && (!best || weight > best.weight))
            best = { language: base, weight };
    }
    return { language: best?.language, rejected };
}

export function negotiatedLanguage(
    cookie: string | undefined,
    acceptLanguage: string,
    country: string | null | undefined,
): Language {
    if (isLanguage(cookie)) return cookie;
    const browser = browserLanguage(acceptLanguage);
    if (browser.language) return browser.language;
    const fallback: Language = country === "TR" ? "tr" : defaultLanguage;
    return browser.rejected.has(fallback) || browser.rejected.has("*")
        ? defaultLanguage
        : fallback;
}
