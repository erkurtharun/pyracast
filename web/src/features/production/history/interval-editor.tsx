"use client";
import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { QUALITIES, qualityOf } from "@/lib/production";
import { ControlField, WriteFailure } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import type { Draft, Editor, Interval } from "./model";
import { repeatsAttempt } from "./screen";

export type DraftChange = Partial<Pick<Draft, "energy" | "quality">>;

export function IntervalEditor({
    row,
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
    const provider = row.provider_originals.length
        ? row.provider_originals.reduce(
              (total, original) => total + original.energy_mwh,
              0,
          )
        : null;
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
                <button
                    className="button secondary"
                    type="button"
                    disabled={blocked}
                    aria-label={
                        !canManage
                            ? proposals("proposeRevertInterval", { interval })
                            : provider === null
                              ? t("remove_correction_interval", { interval })
                              : t("revert_interval", { interval })
                    }
                    onClick={() => onSave("revert")}
                >
                    {!canManage
                        ? proposals("proposeRevert")
                        : provider === null
                          ? t("remove_correction")
                          : t("use_provider", {
                                energy: format.number(provider),
                            })}
                </button>
            )}
            {editor.status === "failed" && (
                <>
                    {editor.issue === "invalid_energy" ? (
                        <Notice error>{t("invalid_energy")}</Notice>
                    ) : (
                        <WriteFailure
                            error={editor.issue.error}
                            repeatable={
                                canManage && !saving && repeatsAttempt(editor)
                            }
                        />
                    )}
                    {editor.issue !== "invalid_energy" &&
                        editor.issue.implausible && (
                            <button
                                className="button"
                                type="button"
                                onClick={() => onSave("set", true)}
                            >
                                {t("save_implausible")}
                            </button>
                        )}
                    <button
                        className="button secondary"
                        type="button"
                        onClick={onReload}
                    >
                        {t("reload_values")}
                    </button>
                </>
            )}
        </form>
    );
}
