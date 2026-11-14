"use client";
import { useLocale, useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { request } from "@/lib/api";
import { parseProduction, type Observation } from "@/lib/production";
import { ActionForm, Notice, Select } from "./ui";

type CsvSelection =
    | { status: "empty" }
    | { status: "invalid"; error: string }
    | { status: "ready"; fileName: string; observations: Observation[] };

type ImportResult = { created: number; existing: number; count: number };

export function ProductionImport({
    base,
    configuration,
    token,
}: {
    base: string;
    configuration: number;
    token: string;
}) {
    const t = useTranslations("ProductionImport");
    const locale = useLocale();

    const fileReadSequence = useRef(0);
    const [selection, setSelection] = useState<CsvSelection>({
        status: "empty",
    });
    const [result, setResult] = useState<ImportResult>();
    const observationCount =
        selection.status === "ready" ? selection.observations.length : 0;

    async function selectFile(file?: File) {
        const sequence = ++fileReadSequence.current;
        setSelection({ status: "empty" });
        setResult(undefined);
        if (!file) return;
        if (file.size > 2 * 1024 * 1024) {
            setSelection({
                status: "invalid",
                error: t("choose_a_csv_no_larger_than_2"),
            });
            return;
        }
        try {
            const text = await file.text();
            if (sequence !== fileReadSequence.current) return;
            setSelection({
                status: "ready",
                fileName: file.name,
                observations: parseProduction(text, locale),
            });
        } catch (error) {
            if (sequence !== fileReadSequence.current) return;
            setSelection({
                status: "invalid",
                error:
                    error instanceof Error
                        ? error.message
                        : t("could_not_read_the_production_csv"),
            });
        }
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("import_production_csv")}</h2>
                </div>
            </div>
            <details>
                <summary>{t("csv_format_and_import_rules")}</summary>
                <p>{t("use_a_csv_with_the_exact_header")}</p>
                <div className="csv-example">
                    <code>
                        starts_at,ends_at,energy,quality,source
                        <br />
                        2026-01-01T09:00:00+03:00,2026-01-01T09:30:00+03:00,250,valid,meter
                    </code>
                </div>
                <p className="fine">
                    {t("quality_valid_outage_curtailed_or_invalid_quoted")}
                </p>
            </details>
            <ActionForm
                submit={t("import_observations", { count: observationCount })}
                onSubmit={async (data) => {
                    if (selection.status !== "ready" || !observationCount)
                        throw new Error(
                            t("choose_and_validate_a_csv_before_importing"),
                        );
                    const response = await request<ImportResult>(
                        `${base}observations/`,
                        "POST",
                        {
                            configuration_id: configuration,
                            unit: data.get("unit"),
                            observations: selection.observations,
                        },
                        token,
                    );
                    setResult(response);
                }}
            >
                <div className="form-grid">
                    <label className="field">
                        {t("production_csv")}
                        <input
                            type="file"
                            accept=".csv,text/csv"
                            required
                            onChange={(event) =>
                                selectFile(event.target.files?.[0])
                            }
                        />
                    </label>
                    <Select label={t("energy_unit")} name="unit">
                        <option value="kWh">{t("kwh_kilowatthours")}</option>
                        <option value="MWh">{t("mwh_megawatthours")}</option>
                    </Select>
                </div>
                {selection.status === "invalid" && (
                    <Notice error>{selection.error}</Notice>
                )}
                {selection.status === "ready" && observationCount > 0 && (
                    <Notice>
                        {t("validated_file", {
                            file: selection.fileName,
                            count: observationCount,
                            start: selection.observations[0].starts_at,
                        })}
                    </Notice>
                )}
            </ActionForm>
            {result && (
                <Notice>
                    {t("import_complete", {
                        created: result.created,
                        existing: result.existing,
                        total: result.count,
                    })}
                </Notice>
            )}
        </section>
    );
}
