"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import type { components } from "@/lib/schema";
import { displayMessage, UnconfirmedError } from "@/lib/api/errors";
import { useFormat } from "@/lib/format/use-format";
import { useAttempt } from "@/lib/idempotency";
import { useLeaveGuard } from "@/lib/leave-guard";
import {
    importExample,
    parseProduction,
    productionWords,
    type ProductionFile,
} from "@/lib/production";
import {
    ActionForm,
    CheckboxField,
    ControlField,
    UnconfirmedNotice,
    useErrorMessage,
} from "@/ui/form";
import { Notice } from "@/ui/ui";
import { ScrollRegion } from "@/ui/scroll-region";
import { useLabel } from "@/ui/vocabulary";

type Preview = components["schemas"]["ProductionPreviewOutput"];
type Selection =
    | { status: "empty" }
    | { status: "reading" }
    | { status: "invalid"; error: string }
    | ({ status: "ready"; fileName: string; sha256: string } & ProductionFile)
    | ({
          status: "preview";
          fileName: string;
          sha256: string;
          preview: Preview;
      } & ProductionFile);
type ImportResult = components["schemas"]["ObservationImportOutput"];
type Previewed = Extract<Selection, { status: "preview" }>;

function importBody(
    selection: Previewed,
    configuration: number,
    implausible: boolean,
) {
    return {
        configuration_id: configuration,
        unit: selection.unit,
        changes: selection.preview.changes,
        origin: "csv_import" as const,
        file_name: selection.fileName.slice(0, 255),
        file_sha256: selection.sha256,
        confirm_implausible: implausible,
    };
}

async function fileSha256(file: File) {
    const digest = await crypto.subtle.digest(
        "SHA-256",
        await file.arrayBuffer(),
    );
    return Array.from(new Uint8Array(digest), (byte) =>
        byte.toString(16).padStart(2, "0"),
    ).join("");
}

export function ProductionImport({
    scope,
    configuration,
    timezone,
    onChanged,
}: {
    scope: PlantScope;
    configuration: number;
    timezone: string;
    onChanged: () => void;
}) {
    const t = useTranslations("ProductionImport");
    const common = useTranslations("Common");
    const csvTexts = useTranslations("ProductionCsv");
    const csvFile = useTranslations("CsvFile");
    const label = useLabel();
    const [header, example] = importExample(
        csvFile,
        label("ObservationQuality", "valid"),
    );
    const format = useFormat();
    const errorMessage = useErrorMessage();
    const attempt = useAttempt<ReturnType<typeof importBody>>();
    const sequence = useRef(0);
    const [selection, setSelection] = useState<Selection>({ status: "empty" });
    const [result, setResult] = useState<ImportResult>();
    const [confirming, setConfirming] = useState(false);
    const [previewRequest, setPreviewRequest] = useState<
        | { status: "idle" | "pending" }
        | { status: "failed"; error: string; retry: boolean }
    >({ status: "idle" });
    const previewError = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (previewRequest.status === "failed") previewError.current?.focus();
    }, [previewRequest]);
    const ready =
        selection.status === "ready" || selection.status === "preview";
    const previewed =
        selection.status === "preview" && previewRequest.status === "idle";
    useLeaveGuard(
        selection.status === "reading" ||
            selection.status === "preview" ||
            (selection.status === "ready" && !result),
    );
    async function selectFile(file?: File) {
        const current = ++sequence.current;
        setResult(undefined);
        setPreviewRequest({ status: "idle" });
        if (!file) {
            setSelection({ status: "empty" });
            return;
        }
        if (file.size > 2 * 1024 * 1024) {
            setSelection({
                status: "invalid",
                error: t("choose_a_csv_no_larger_than_2"),
            });
            return;
        }
        setSelection({ status: "reading" });
        try {
            const parsed = parseProduction(
                await file.text(),
                csvTexts,
                await productionWords(),
            );
            const sha256 = await fileSha256(file);
            if (current === sequence.current)
                setSelection({
                    status: "ready",
                    fileName: file.name,
                    sha256,
                    ...parsed,
                });
        } catch (error) {
            if (current === sequence.current)
                setSelection({
                    status: "invalid",
                    error: displayMessage(
                        error,
                        t("could_not_read_the_production_csv"),
                    ),
                });
        }
    }
    const date = (value: string) =>
        format.time(value, timezone, {
            year: "numeric",
            month: "short",
            day: "2-digit",
        });
    const timeFormatter = format.intervals(
        timezone,
        selection.status === "preview"
            ? [
                  ...selection.preview.preview,
                  ...(selection.preview.warnings ?? []),
              ].flatMap((row) => [row.starts_at, row.ends_at])
            : [],
    );
    const time = (value: string) => timeFormatter.format(new Date(value));
    const implausible =
        selection.status === "preview" &&
        (selection.preview.warnings ?? []).some(
            (warning) => warning.code === "energy_exceeds_capacity",
        );
    return (
        <section>
            <details>
                <summary>{t("csv_format_and_import_rules")}</summary>
                <p>
                    {t("use_a_csv_with_the_exact_header", {
                        kwh: csvFile("columns.actual_kwh"),
                    })}
                </p>
                <div className="csv-example">
                    <code>
                        {header}
                        <br />
                        {example}
                    </code>
                </div>
                <p className="fine">
                    {t("quality_valid_outage_curtailed_or_invalid_quoted", {
                        status: csvFile("columns.status"),
                        source: csvFile("columns.source"),
                    })}
                </p>
            </details>
            <form
                onSubmit={(event) =>
                    void (async () => {
                        event.preventDefault();
                        if (confirming || previewRequest.status === "pending")
                            return;
                        if (!ready) {
                            setPreviewRequest({
                                status: "failed",
                                error: t(
                                    "choose_and_validate_a_csv_before_importing",
                                ),
                                retry: false,
                            });
                            return;
                        }
                        const current = ++sequence.current;
                        setPreviewRequest({ status: "pending" });
                        try {
                            const preview = await api.post(
                                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/preview/",
                                {
                                    path: scope,
                                    body: {
                                        configuration_id: configuration,
                                        unit: selection.unit,
                                        observations: selection.observations,
                                    },
                                },
                            );
                            if (current === sequence.current) {
                                setSelection({
                                    ...selection,
                                    status: "preview",
                                    preview,
                                });
                                setPreviewRequest({ status: "idle" });
                            }
                        } catch (error) {
                            if (current === sequence.current)
                                setPreviewRequest({
                                    status: "failed",
                                    error: errorMessage(error),
                                    retry: true,
                                });
                        }
                    })()
                }
            >
                <fieldset
                    disabled={confirming || previewRequest.status === "pending"}
                >
                    {previewRequest.status === "failed" && (
                        <div
                            className="notice error"
                            role="alert"
                            tabIndex={-1}
                            ref={previewError}
                        >
                            {previewRequest.error}
                            {previewRequest.retry && (
                                <button
                                    className="button secondary"
                                    type="submit"
                                >
                                    {common("retry")}
                                </button>
                            )}
                        </div>
                    )}
                    <div className="form-grid">
                        <ControlField label={t("production_csv")}>
                            <input
                                type="file"
                                accept=".csv,text/csv"
                                required
                                disabled={confirming}
                                onChange={(event) =>
                                    void selectFile(event.target.files?.[0])
                                }
                            />
                        </ControlField>
                    </div>
                    {selection.status === "invalid" && (
                        <Notice error>{selection.error}</Notice>
                    )}
                    {ready && (
                        <Notice>
                            {t("validated_file", {
                                file: selection.fileName,
                                count: selection.observations.length,
                            })}
                        </Notice>
                    )}
                    <div className="form-footer">
                        <button className="button" type="submit">
                            {previewRequest.status === "pending"
                                ? t("previewing")
                                : t("preview_import")}
                        </button>
                    </div>
                </fieldset>
            </form>
            {attempt.outstanding && !previewed && (
                <UnconfirmedNotice repeatable={false} />
            )}
            {previewed && (
                <>
                    {(selection.preview.warnings ?? []).map((warning) => (
                        <Notice key={`${warning.code}/${warning.starts_at}`}>
                            {t(warning.code, {
                                start: format.time(warning.starts_at, timezone),
                                end: format.time(warning.ends_at, timezone),
                            })}
                        </Notice>
                    ))}
                    <ScrollRegion label={t("import_preview")}>
                        <table className="production-preview-table">
                            <caption>{t("import_preview")}</caption>
                            <thead>
                                <tr>
                                    <th>{t("interval")}</th>
                                    <th>{t("current_mwh")}</th>
                                    <th>{t("new_mwh")}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {selection.preview.preview.map((row) => (
                                    <tr key={`${row.starts_at}/${row.ends_at}`}>
                                        <th scope="row">
                                            <span className="production-preview-date">
                                                {date(row.starts_at)}
                                            </span>
                                            <span className="production-preview-time">
                                                {time(row.starts_at)}–
                                                {time(row.ends_at)}
                                            </span>
                                            <span className="sr-only">
                                                {t("exact_interval", {
                                                    start: row.starts_at,
                                                    end: row.ends_at,
                                                })}
                                            </span>
                                        </th>
                                        <td>
                                            {row.current_energy_mwh === null
                                                ? t("missing")
                                                : format.number(
                                                      row.current_energy_mwh,
                                                  )}
                                        </td>
                                        <td>
                                            {format.number(row.new_energy_mwh)}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </ScrollRegion>
                    <ActionForm
                        submit={t("confirm_import", {
                            count: selection.preview.changes.length,
                        })}
                        attempt={
                            attempt.outstanding && {
                                repeats: (data) =>
                                    attempt.repeats(
                                        importBody(
                                            selection,
                                            configuration,
                                            data.has("confirm_implausible"),
                                        ),
                                    ),
                            }
                        }
                        onSubmit={async (data) => {
                            if (confirming) return;
                            const current = ++sequence.current;
                            const body = importBody(
                                selection,
                                configuration,
                                data.has("confirm_implausible"),
                            );
                            const sending = attempt.next(body);
                            setConfirming(true);
                            try {
                                const response = await api
                                    .post(
                                        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/changes/",
                                        { path: scope, body },
                                    )
                                    .catch((error: unknown) => {
                                        attempt.settle(sending, error);
                                        if (error instanceof UnconfirmedError)
                                            onChanged();
                                        throw error;
                                    });
                                attempt.settle(sending);
                                if (current === sequence.current) {
                                    setResult(response);
                                    setSelection({
                                        status: "ready",
                                        fileName: selection.fileName,
                                        sha256: selection.sha256,
                                        unit: selection.unit,
                                        observations: selection.observations,
                                    });
                                    onChanged();
                                }
                            } finally {
                                setConfirming(false);
                            }
                        }}
                    >
                        {implausible && (
                            <CheckboxField
                                label={t("confirm_implausible")}
                                name="confirm_implausible"
                                required
                            />
                        )}
                    </ActionForm>
                </>
            )}
            {result && (
                <Notice>
                    {t("import_complete", {
                        created: result.created,
                        existing: result.existing,
                    })}
                </Notice>
            )}
        </section>
    );
}
