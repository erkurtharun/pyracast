import { createTranslator } from "next-intl";
import { isLanguage } from "./language";
import { messages } from "./messages";

export function browserErrors() {
    const language =
        typeof document === "undefined" ? "en" : document.documentElement.lang;
    const locale = isLanguage(language) ? language : "en";
    return createTranslator({ locale, messages: messages[locale].Errors });
}
