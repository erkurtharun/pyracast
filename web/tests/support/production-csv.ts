import { createTranslator } from "next-intl";
import type { Language } from "../../src/i18n/language";
import { messages } from "../../src/i18n/messages";

export function csvTexts(locale: Language = "en") {
    return createTranslator({
        locale,
        messages: messages[locale].ProductionCsv,
    });
}
