"use client";
import { useTranslations } from "next-intl";
import { Fragment, type ReactNode, type RefObject } from "react";
import { PendingApproval } from "@/features/inbox/pending-approval";
import { ScrollRegion } from "@/ui/scroll-region";
import { intervalKey, type Interval } from "./model";
import { ProductionReading } from "./production-reading";

export function ProductionTable({
    tenantId,
    rows,
    visibleRows,
    timeFormatter,
    intervalLabel,
    canManage,
    canAct,
    hasConfiguration,
    editorOpen,
    editingKey,
    editButtons,
    unavailable,
    onOpen,
    renderEditor,
}: {
    tenantId: number;
    rows: Interval[];
    visibleRows: Interval[];
    timeFormatter: Intl.DateTimeFormat;
    intervalLabel: (row: Interval) => string;
    canManage: boolean;
    canAct: (row: Interval) => boolean;
    hasConfiguration: boolean;
    editorOpen: boolean;
    editingKey: string | undefined;
    editButtons: RefObject<Map<string, HTMLButtonElement>>;
    unavailable: (row: Interval) => string;
    onOpen: (row: Interval) => void;
    renderEditor: (row: Interval) => ReactNode;
}) {
    const t = useTranslations("ProductionHistory");
    const proposals = useTranslations("ChangeRequests");
    const actions =
        canManage ||
        rows.some((row) => row.can_propose || row.pending_request_id != null);
    return (
        <ScrollRegion label={t("production_intervals")}>
            <table className="production-table">
                <caption>{t("production_intervals")}</caption>
                <thead>
                    <tr>
                        <th>{t("local_interval")}</th>
                        <th>{t("production_mwh")}</th>
                        {actions && <th>{t("actions")}</th>}
                    </tr>
                </thead>
                <tbody>
                    {visibleRows.map((row) => {
                        const key = intervalKey(row);
                        return (
                            <Fragment key={key}>
                                <tr>
                                    <th scope="row">{intervalLabel(row)}</th>
                                    <td>
                                        <ProductionReading
                                            row={row}
                                            timeFormatter={timeFormatter}
                                        />
                                    </td>
                                    {actions && (
                                        <td>
                                            {canAct(row) && hasConfiguration ? (
                                                <button
                                                    ref={(button) => {
                                                        if (button)
                                                            editButtons.current.set(
                                                                key,
                                                                button,
                                                            );
                                                        else
                                                            editButtons.current.delete(
                                                                key,
                                                            );
                                                    }}
                                                    className="button secondary"
                                                    type="button"
                                                    disabled={editorOpen}
                                                    aria-label={t(
                                                        canManage
                                                            ? "edit_interval"
                                                            : "propose_interval",
                                                        {
                                                            interval:
                                                                intervalLabel(
                                                                    row,
                                                                ),
                                                        },
                                                    )}
                                                    onClick={() => onOpen(row)}
                                                >
                                                    {canManage
                                                        ? t("edit")
                                                        : proposals(
                                                              "proposeCorrection",
                                                          )}
                                                </button>
                                            ) : (
                                                <span className="fine">
                                                    {unavailable(row)}
                                                </span>
                                            )}
                                            {row.pending_request_id != null && (
                                                <PendingApproval
                                                    tenantId={tenantId}
                                                    requestId={
                                                        row.pending_request_id
                                                    }
                                                    subject={intervalLabel(row)}
                                                />
                                            )}
                                        </td>
                                    )}
                                </tr>
                                {editingKey === key && (
                                    <tr>
                                        <td colSpan={actions ? 3 : 2}>
                                            {renderEditor(row)}
                                        </td>
                                    </tr>
                                )}
                            </Fragment>
                        );
                    })}
                </tbody>
            </table>
        </ScrollRegion>
    );
}
