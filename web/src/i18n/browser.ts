import { createTranslator } from "next-intl";
import { isLanguage } from "./language";
import { shell } from "./shell";

export function browserErrors() {
    const language =
        typeof document === "undefined" ? "en" : document.documentElement.lang;
    const locale = isLanguage(language) ? language : "en";
    return createTranslator({ locale, messages: shell[locale].Errors });
}
