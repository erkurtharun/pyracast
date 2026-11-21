"use client";

import { useTranslations } from "next-intl";
import { csvDocument, type CsvExport } from "@/lib/csv";

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
    return (
        <button
            type="button"
            className="button secondary"
            disabled={pending}
            onClick={() => {
                const file = build();
                const url = URL.createObjectURL(
                    new Blob([csvDocument(file.header, file.rows)], {
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
