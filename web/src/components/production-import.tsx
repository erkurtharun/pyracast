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
                error: "Choose a CSV no larger than 2 MiB.",
            });
            return;
        }
        try {
            const text = await file.text();
            if (sequence !== fileReadSequence.current) return;
            setSelection({
                status: "ready",
                fileName: file.name,
                observations: parseProduction(text),
            });
        } catch (error) {
            if (sequence !== fileReadSequence.current) return;
            setSelection({
                status: "invalid",
                error:
                    error instanceof Error
                        ? error.message
                        : "Could not read the production CSV.",
            });
        }
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Import production CSV</h2>
                    <p>
                        Import 30 or 60 minute readings covered by archived
                        estimated actuals. Available dates are in the
                        Calibration journal.
                    </p>
                </div>
            </div>
            <details>
                <summary>CSV format and import rules</summary>
                <p>
                    Use a CSV with the exact header below. Timestamps need an
                    explicit timezone, energy must be non-negative, and
                    intervals cannot overlap. Each batch accepts up to 2,000
                    rows.
                </p>
                <div className="csv-example">
                    <code>
                        starts_at,ends_at,energy,quality,source
                        <br />
                        2026-01-01T09:00:00+03:00,2026-01-01T09:30:00+03:00,250,valid,meter
                    </code>
                </div>
                <p className="fine">
                    Quality: valid, outage, curtailed, or invalid. Quoted CSV
                    fields may contain commas and line breaks. Units apply to
                    the whole batch. Exact replays are safe; conflicting records
                    reject the whole batch.
                </p>
            </details>
            <ActionForm
                submit={`Import ${observationCount || ""} observations`}
                onSubmit={async (data) => {
                    if (selection.status !== "ready" || !observationCount)
                        throw new Error(
                            "Choose and validate a CSV before importing.",
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
                        Production CSV
                        <input
                            type="file"
                            accept=".csv,text/csv"
                            required
                            onChange={(event) =>
                                selectFile(event.target.files?.[0])
                            }
                        />
                    </label>
                    <Select label="Energy unit" name="unit">
                        <option value="kWh">kWh — kilowatt-hours</option>
                        <option value="MWh">MWh — megawatt-hours</option>
                    </Select>
                </div>
                {selection.status === "invalid" && (
                    <Notice error>{selection.error}</Notice>
                )}
                {selection.status === "ready" && observationCount > 0 && (
                    <Notice>
                        {selection.fileName}: {observationCount} valid
                        observations ready. First interval:{" "}
                        {selection.observations[0].starts_at}.
                    </Notice>
                )}
            </ActionForm>
            {result && (
                <Notice>
                    Import complete: {result.created} created, {result.existing}{" "}
                    already present, {result.count} total.
                </Notice>
            )}
        </section>
    );
}
