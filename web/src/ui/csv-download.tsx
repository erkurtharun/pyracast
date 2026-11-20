"use client";

import { useTranslations } from "next-intl";
import { csvDocument } from "@/lib/csv";

export function CsvDownload({
    filename,
    records,
    metadata = {},
    label,
}: {
    filename: string;
    records: Record<string, unknown>[];
    metadata?: Record<string, unknown>;
    label?: string;
}) {
    const t = useTranslations("CsvDownload");
    return (
        <button
            type="button"
            className="button secondary"
            onClick={() => {
                const url = URL.createObjectURL(
                    new Blob([csvDocument(records, metadata)], {
                        type: "text/csv;charset=utf-8",
                    }),
                );
                const link = document.createElement("a");
                link.href = url;
                link.download = `${filename.replace(/[^a-zA-Z0-9_.-]/g, "-")}.csv`;
                document.body.append(link);
                link.click();
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}
        >
            {label ?? t("download_csv")}
        </button>
    );
}
