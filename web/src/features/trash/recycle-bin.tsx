"use client";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api/client";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { useApiList, useRefresh } from "@/lib/api/query";
import { routes } from "@/lib/routes";
import { useErrorMessage } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty, Notice } from "@/ui/ui";
import { useForgetDeletion } from "./recent-deletion";

type DeletedRegion = components["schemas"]["TrashedRegionOutput"];
type Restore =
    | { kind: "idle" }
    | { kind: "restoring"; id: number }
    | { kind: "restored"; row: DeletedRegion }
    | { kind: "failed"; message: string };

export function RecycleBin({ tenant }: { tenant: number }) {
    const t = useTranslations("RecycleBin");
    const format = useFormat();
    const errorMessage = useErrorMessage();
    const router = useRouter();
    const refresh = useRefresh();
    const forget = useForgetDeletion();
    const [restore, setRestore] = useState<Restore>({ kind: "idle" });
    // A restored row leaves the list before the reload that drops it arrives.
    const [restored, setRestored] = useState<number[]>([]);
    const regions = useApiList("/api/tenants/{tenant_id}/trash/regions/", {
        path: { tenant_id: tenant },
    });
    const rows =
        regions.data?.results.filter((row) => !restored.includes(row.id)) ?? [];
    async function restoreRow(row: DeletedRegion) {
        setRestore({ kind: "restoring", id: row.id });
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/restore/",
                {
                    path: { tenant_id: tenant, region_id: row.id },
                },
            );
        } catch (failure) {
            setRestore({ kind: "failed", message: errorMessage(failure) });
            void refresh.workspace(tenant);
            return;
        }
        setRestored((current) => [...current, row.id]);
        forget(row.id);
        await refresh.workspace(tenant);
        setRestore({ kind: "restored", row });
    }
    return (
        <>
            <div className="page-heading">
                <div>
                    <h1>{t("recycleBin")}</h1>
                    <p>{t("retentionHelp")}</p>
                </div>
            </div>
            {restore.kind === "failed" && (
                <Notice error>{restore.message}</Notice>
            )}
            {restore.kind === "restored" && (
                <Notice>
                    <p>{t("restored", { name: restore.row.plant_name })}</p>
                    <button
                        className="button secondary"
                        type="button"
                        onClick={() =>
                            router.push(routes.plant(tenant, restore.row.id))
                        }
                    >
                        {t("openPlant")}
                    </button>
                </Notice>
            )}
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
                                            <h3>{row.plant_name}</h3>
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
                                                    restore.kind ===
                                                        "restoring" ||
                                                    row.restore_blocker !== null
                                                }
                                                onClick={() =>
                                                    void restoreRow(row)
                                                }
                                            >
                                                {restore.kind === "restoring" &&
                                                restore.id === row.id
                                                    ? t("restoring")
                                                    : t("restore", {
                                                          name: row.plant_name,
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
