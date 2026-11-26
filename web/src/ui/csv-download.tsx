"use client";

import { useTranslations } from "next-intl";
import {
    csvDocument,
    readable,
    type CsvExport,
    type CsvTexts,
} from "@/lib/csv";
import { useCodeLabel, useLabel } from "./vocabulary";

function useCsvTexts(): CsvTexts {
    const t = useTranslations("CsvFile");
    const performance = useTranslations("ForecastPerformance");
    const label = useLabel();
    const reason = useCodeLabel("ForecastPerformance.reasons");
    return {
        column: (column) => t(`columns.${column}`),
        text: (text) => {
            if (text.kind === "word") return t(`words.${text.word}`);
            if (text.kind === "label") return label(text.group, text.value);
            const missing = performance(
                "production_or_reference_data_is_unavailable",
            );
            return text.reason ? reason(text.reason, missing) : missing;
        },
    };
}

export function CsvDownload({
    build,
    label,
    pending = false,
}: {
    build: () => CsvExport;
    label?: string;
    pending?: boolean;
}) {
    const t = useTranslations("CsvDownload");
    const common = useTranslations("Common");
    const texts = useCsvTexts();
    return (
        <button
            type="button"
            className="button secondary"
            disabled={pending}
            onClick={() => {
                const file = build();
                const { header, rows } = readable(file, texts);
                const url = URL.createObjectURL(
                    new Blob([csvDocument(header, rows)], {
                        type: "text/csv;charset=utf-8",
                    }),
                );
                const link = document.createElement("a");
                link.href = url;
                link.download = file.filename;
                document.body.append(link);
                link.click();
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}
        >
            {pending ? common("working") : (label ?? t("download_csv"))}
        </button>
    );
}
