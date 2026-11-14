import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { isLanguage } from "./language";
import { messages } from "./messages";

export default getRequestConfig(async () => {
    const preference = (await cookies()).get("pyracast_language")?.value;
    const locale = isLanguage(preference) ? preference : "en";
    return { locale, messages: messages[locale], timeZone: "UTC" };
});
