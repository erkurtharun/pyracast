"use client";
import { useTranslations } from "next-intl";
import { QUALITIES, qualityOf, type Quality } from "@/lib/production";
import { ControlField } from "@/ui/form";
import { Notice } from "@/ui/ui";
import type { Draft, Editor, Interval } from "./model";

export type DraftChange = Partial<Pick<Draft, "energy" | "quality" | "reason">>;

export function IntervalEditor({
    row,
    editor,
    interval,
    canManage,
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
    saving: boolean;
    onChange: (change: DraftChange) => void;
    onSave: (action: "set" | "revert") => void;
    onCancel: () => void;
    onReload: () => void;
}) {
    const t = useTranslations("ProductionHistory");
    const proposals = useTranslations("ChangeRequests");
    const qualityLabel = (quality: Quality) =>
        ({
            valid: t("valid"),
            invalid: t("invalid"),
            outage: t("outage"),
            curtailed: t("curtailed"),
        })[quality];
    const blocked = saving || (canManage && editor.status === "failed");
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
                    autoFocus
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
                            {qualityLabel(quality)}
                        </option>
                    ))}
                </select>
            </ControlField>
            {!canManage && (
                <ControlField label={proposals("reason")}>
                    <textarea
                        required
                        maxLength={500}
                        value={editor.reason ?? ""}
                        disabled={saving}
                        onChange={(event) =>
                            onChange({ reason: event.target.value })
                        }
                    />
                    <span className="field-hint">
                        {proposals("approvalHelp")}
                    </span>
                </ControlField>
            )}
            <div className="button-group">
                <button className="button" type="submit" disabled={blocked}>
                    {saving
                        ? t("saving")
                        : canManage
                          ? t("save")
                          : proposals("send")}
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
            {(canManage ? row.can_revert : row.can_propose_revert) && (
                <button
                    className="button secondary"
                    type="button"
                    disabled={blocked}
                    aria-label={
                        canManage
                            ? t("revert_interval", { interval })
                            : proposals("proposeRevertInterval", { interval })
                    }
                    onClick={() => onSave("revert")}
                >
                    {canManage ? t("use_provider") : proposals("proposeRevert")}
                </button>
            )}
            {editor.status === "failed" && (
                <>
                    <Notice error>{editor.error}</Notice>
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
