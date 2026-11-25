import { createTranslator } from "next-intl";
import type { CsvTexts } from "../../src/lib/csv";
import { importWords } from "../../src/lib/production";
import type { Language } from "../../src/i18n/language";
import { messages } from "../../src/i18n/messages";

export function csvTexts(locale: Language = "en") {
    return [
        createTranslator({ locale, messages: messages[locale].ProductionCsv }),
        importWords([messages.en, messages.tr]),
    ] as const;
}

export function fileTexts(locale: Language = "en"): CsvTexts {
    const catalogue = messages[locale];
    const file = createTranslator({ locale, messages: catalogue.CsvFile });
    const labels = createTranslator({ locale, messages: catalogue.Enums });
    const performance = createTranslator({
        locale,
        messages: catalogue.ForecastPerformance,
    });
    return {
        column: (column) => file(`columns.${column}`),
        text: (text) =>
            text.kind === "word"
                ? file(`words.${text.word}`)
                : text.kind === "label"
                  ? labels(`${text.group}.${text.value}` as never)
                  : text.reason
                    ? performance(`reasons.${text.reason}` as never)
                    : performance(
                          "production_or_reference_data_is_unavailable",
                      ),
    };
}
