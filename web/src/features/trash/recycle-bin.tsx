"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { useApiList, useRefresh } from "@/lib/api/query";
import { useErrorMessage } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty, Notice } from "@/ui/ui";

type DeletedRegion = components["schemas"]["TrashedRegionOutput"];

export function MoveToRecycleBin({
    name,
    scope,
    onDeleted,
}: {
    name: string;
    scope: RegionScope;
    onDeleted: () => void;
}) {
    const t = useTranslations("RecycleBin");
    const errorMessage = useErrorMessage();
    const [confirming, setConfirming] = useState(false);
    const [working, setWorking] = useState(false);
    const [error, setError] = useState("");
    if (!confirming)
        return (
            <button
                className="button secondary"
                onClick={() => setConfirming(true)}
            >
                {t("deletePlant")}
            </button>
        );
    return (
        <section className="notice" aria-label={t("deletePlantConfirmation")}>
            <h2>{t("confirmMove", { name })}</h2>
            <p>{t("deletionHelp")}</p>
            {error && <Notice error>{error}</Notice>}
            <div className="button-group">
                <button
                    className="button secondary"
                    disabled={working}
                    onClick={() => {
                        setConfirming(false);
                        setError("");
                    }}
                >
                    {t("cancel")}
                </button>
                <button
                    className="button danger"
                    disabled={working}
                    onClick={async () => {
                        setWorking(true);
                        setError("");
                        try {
                            await api.delete(
                                "/api/tenants/{tenant_id}/regions/{region_id}/",
                                { path: scope },
                            );
                            onDeleted();
                        } catch (failure) {
                            setError(errorMessage(failure));
                            setWorking(false);
                        }
                    }}
                >
                    {working ? t("moving") : t("movePlantToRecycleBin")}
                </button>
            </div>
        </section>
    );
}

export function RecycleBin({ tenant }: { tenant: number }) {
    const t = useTranslations("RecycleBin");
    const format = useFormat();
    const errorMessage = useErrorMessage();

    const refresh = useRefresh();
    const [working, setWorking] = useState<number | null>(null);
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [restored, setRestored] = useState<number[]>([]);
    const regions = useApiList("/api/tenants/{tenant_id}/trash/regions/", {
        path: { tenant_id: tenant },
    });
    const rows =
        regions.data?.results.filter((row) => !restored.includes(row.id)) ?? [];
    async function restore(row: DeletedRegion) {
        setWorking(row.id);
        setError("");
        setMessage("");
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/restore/",
                {
                    path: { tenant_id: tenant, region_id: row.id },
                },
            );
            setRestored((current) => [...current, row.id]);
            await refresh.workspace(tenant);
            setMessage(t("restored", { name: row.name }));
        } catch (failure) {
            setError(errorMessage(failure));
            void refresh.workspace(tenant);
        } finally {
            setWorking(null);
        }
    }
    return (
        <>
            <div className="page-heading">
                <div>
                    <h1>{t("recycleBin")}</h1>
                    <p>{t("retentionHelp")}</p>
                </div>
            </div>
            {error && <Notice error>{error}</Notice>}
            {message && <Notice>{message}</Notice>}
            <section className="panel">
                <h2>{t("deletedPlants")}</h2>
                <Resource
                    resource={regions}
                    loading={t("loadingDeletedPlants")}
                >
                    {() =>
                        rows.length ? (
                            <ul
                                className="trash-list"
                                aria-label={t("deletedPlants")}
                            >
                                {rows.map((row) => (
                                    <li className="trash-item" key={row.id}>
                                        <div>
                                            <strong>{row.name}</strong>
                                            <dl className="trash-meta">
                                                <div>
                                                    <dt>{t("deleted")}</dt>
                                                    <dd>
                                                        <time
                                                            dateTime={
                                                                row.deleted_at
                                                            }
                                                        >
                                                            {format.deviceTime(
                                                                row.deleted_at,
                                                            )}
                                                        </time>
                                                    </dd>
                                                </div>
                                                <div>
                                                    <dt>
                                                        {t("permanentDeletion")}
                                                    </dt>
                                                    <dd>
                                                        <time
                                                            dateTime={
                                                                row.purge_after
                                                            }
                                                        >
                                                            {format.deviceTime(
                                                                row.purge_after,
                                                            )}
                                                        </time>
                                                    </dd>
                                                </div>
                                            </dl>
                                        </div>
                                        <div>
                                            <button
                                                className="button secondary"
                                                disabled={
                                                    working !== null ||
                                                    row.restore_blocker !== null
                                                }
                                                onClick={() =>
                                                    void restore(row)
                                                }
                                            >
                                                {working === row.id
                                                    ? t("restoring")
                                                    : t("restore", {
                                                          name: row.name,
                                                      })}
                                            </button>
                                            {row.restore_blocker && (
                                                <p className="fine">
                                                    {t(
                                                        `blockers.${row.restore_blocker}`,
                                                    )}
                                                </p>
                                            )}
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        ) : (
                            <Empty title={t("noDeletedPlants")}>
                                {t("nothingToRestore")}
                            </Empty>
                        )
                    }
                </Resource>
            </section>
        </>
    );
}
