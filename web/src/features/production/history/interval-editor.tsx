"use client";
import { useEffect, useId, useRef } from "react";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { QUALITIES, qualityOf } from "@/lib/production";
import { UnconfirmedError } from "@/lib/api/errors";
import { ControlField, UnconfirmedNotice, WriteFailure } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import { revertOutcome, type Draft, type Editor, type Interval } from "./model";
import { repeatsAttempt } from "./screen";

export type DraftChange = Partial<Pick<Draft, "energy" | "quality">>;

export function IntervalEditor({
    row,
    rows,
    editor,
    interval,
    canManage,
    staged,
    saving,
    onChange,
    onSave,
    onCancel,
    onReload,
}: {
    row: Interval;
    rows: readonly Interval[];
    editor: Exclude<Editor, { status: "closed" }>;
    interval: string;
    canManage: boolean;
    staged: boolean;
    saving: boolean;
    onChange: (change: DraftChange) => void;
    onSave: (action: "set" | "revert", confirmImplausible?: boolean) => void;
    onCancel: () => void;
    onReload: () => void;
}) {
    const t = useTranslations("ProductionHistory");
    const proposals = useTranslations("ChangeRequests");
    const label = useLabel();
    const energyRef = useRef<HTMLInputElement>(null);
    useEffect(() => energyRef.current?.focus(), []);
    // A failure ends the editor's writes until an edit or a reload, unless a write is still outstanding.
    const blocked =
        saving || (canManage && editor.status === "failed" && !editor.attempt);
    const format = useFormat();
    const outcome = revertOutcome(row, rows);
    const consequenceId = useId();
    return (
        <form
            className="production-row-editor"
            onSubmit={(event) => {
                event.preventDefault();
                onSave("set");
            }}
        >
            <ControlField label={t("edit_energy", { interval })}>
                <input
                    inputMode="decimal"
                    ref={energyRef}
                    value={editor.energy}
                    disabled={saving}
                    onChange={(event) =>
                        onChange({ energy: event.target.value })
                    }
                />
            </ControlField>
            <ControlField label={t("quality")}>
                <select
                    value={editor.quality}
                    disabled={saving}
                    onChange={(event) =>
                        onChange({ quality: qualityOf(event.target.value) })
                    }
                >
                    {QUALITIES.map((quality) => (
                        <option key={quality} value={quality}>
                            {label("ObservationQuality", quality)}
                        </option>
                    ))}
                </select>
            </ControlField>
            <div className="button-group">
                <button className="button" type="submit" disabled={blocked}>
                    {saving
                        ? t("saving")
                        : canManage
                          ? t("save")
                          : staged
                            ? proposals("updateInRequest")
                            : proposals("addToRequest")}
                </button>
                <button
                    className="button secondary"
                    type="button"
                    disabled={saving}
                    onClick={onCancel}
                >
                    {t("cancel")}
                </button>
            </div>
            {(canManage
                ? row.can_revert || editor.attempt?.action === "revert"
                : row.can_propose_revert) && (
                <>
                    <button
                        className="button secondary"
                        type="button"
                        disabled={blocked}
                        aria-label={
                            outcome.kind === "restore"
                                ? t("revert_interval", {
                                      interval,
                                      energy: format.number(outcome.energy),
                                  })
                                : t("remove_correction_interval", { interval })
                        }
                        aria-describedby={
                            outcome.kind === "restore"
                                ? undefined
                                : consequenceId
                        }
                        onClick={() => onSave("revert")}
                    >
                        {outcome.kind === "restore"
                            ? t("use_provider", {
                                  energy: format.number(outcome.energy),
                              })
                            : t("remove_correction")}
                    </button>
                    {outcome.kind !== "restore" && (
                        <p className="fine" id={consequenceId}>
                            {outcome.kind === "none"
                                ? t("remove_consequence")
                                : t("entered_removed_consequence")}
                        </p>
                    )}
                </>
            )}
            {editor.attempt && (
                <UnconfirmedNotice
                    repeatable={canManage && !saving && repeatsAttempt(editor)}
                />
            )}
            {editor.status === "failed" &&
                (editor.issue === "invalid_energy" ? (
                    <Notice error>{t("invalid_energy")}</Notice>
                ) : (
                    <>
                        {!(editor.issue.error instanceof UnconfirmedError) && (
                            <WriteFailure error={editor.issue.error} />
                        )}
                        {editor.issue.implausible && (
                            <button
                                className="button"
                                type="button"
                                onClick={() => onSave("set", true)}
                            >
                                {t("save_implausible")}
                            </button>
                        )}
                    </>
                ))}
            {(editor.status === "failed" ||
                (editor.status === "editing" && editor.attempt)) && (
                <button
                    className="button secondary"
                    type="button"
                    onClick={onReload}
                >
                    {t("reload_values")}
                </button>
            )}
        </form>
    );
}
