import { cookies, headers } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { isLanguage } from "./language";
import { messages } from "./messages";

export default getRequestConfig(async () => {
    const publicLanguage = (await headers()).get("x-pyracast-public-language");
    if (isLanguage(publicLanguage))
        return {
            locale: publicLanguage,
            messages: messages[publicLanguage],
            timeZone: "UTC",
        };
    const preference = (await cookies()).get("pyracast_language")?.value;
    const locale = isLanguage(preference) ? preference : "en";
    return { locale, messages: messages[locale], timeZone: "UTC" };
});
