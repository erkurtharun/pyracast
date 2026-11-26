import { headers } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { isLanguage } from "./language";
import { messages } from "./messages";
import { defaultLanguage, languageHeader } from "./request-language";

export default getRequestConfig(async () => {
    const resolved = (await headers()).get(languageHeader);
    const locale = isLanguage(resolved) ? resolved : defaultLanguage;
    return { locale, messages: messages[locale], timeZone: "UTC" };
});
