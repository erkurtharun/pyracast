"use client";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { cadence, useApiList, useRefresh } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import { routes } from "@/lib/routes";
import { PlantIcon } from "./plant-icon";
import { PlantOnboarding } from "./plant-onboarding";
import { Field, Select } from "@/ui/form";
import { Empty, Notice, RefreshFailed } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import { useWorkspace } from "@/features/workspace/workspace-shell";

export function Portfolio() {
    const t = useTranslations("Dashboard");
    const locale = useLocale();
    const format = useFormat();
    const label = useLabel();
    const router = useRouter();
    const refresh = useRefresh();
    const { tenant } = useWorkspace();
    const canManage = tenant.role === "admin";
    const [creating, setCreating] = useState(false);
    const [search, setSearch] = useState("");
    const [status, setStatus] = useState("all");
    const sites = useApiList(
        "/api/tenants/{tenant_id}/sites/",
        { path: { tenant_id: tenant.id } },
        { poll: cadence.live },
    );
    const rows = sites.data?.results ?? [];
    const offerCreate = canManage && !creating && !!sites.data;
    const open = (site: number) => router.push(routes.plant(tenant.id, site));
    const filtered = rows.filter(
        (site) =>
            (status === "all" || site.status === status) &&
            `${site.name} ${site.plant?.name ?? ""} ${site.timezone}`
                .toLocaleLowerCase(locale)
                .includes(search.toLocaleLowerCase(locale)),
    );
    return (
        <>
            <div className="page-heading">
                <div>
                    <p className="eyebrow name">{tenant.name}</p>
                    <h1>
                        {t("plants")}{" "}
                        {sites.data && (
                            <span className="count" aria-hidden="true">
                                {rows.length}
                            </span>
                        )}
                    </h1>
                </div>
                {offerCreate && rows.length > 0 && (
                    <button
                        className="button"
                        onClick={() => setCreating(true)}
                    >
                        {t("addPlant")}
                    </button>
                )}
            </div>
            {canManage && creating && (
                <PlantOnboarding
                    tenant={tenant.id}
                    onCancel={() => setCreating(false)}
                    onCreated={async (site) => {
                        await refresh.workspace(tenant.id);
                        open(site.id);
                    }}
                />
            )}
            <section className="panel" aria-label={t("plants")}>
                {rows.length > 0 && (
                    <div className="portfolio-toolbar">
                        <Field
                            label={t("searchPlants")}
                            name="search"
                            type="search"
                            value={search}
                            onChange={(event) => setSearch(event.target.value)}
                            placeholder={t("searchPlaceholder")}
                        />
                        <Select
                            label={t("approvalStatus")}
                            value={status}
                            onChange={(event) => setStatus(event.target.value)}
                        >
                            <option value="all">{t("allStatuses")}</option>
                            {(
                                [
                                    "pending",
                                    "approved",
                                    "rejected",
                                    "suspended",
                                ] as const
                            ).map((value) => (
                                <option key={value} value={value}>
                                    {label("RegionStatus", value)}
                                </option>
                            ))}
                        </Select>
                    </div>
                )}
                <RefreshFailed resource={sites} />
                {sites.error && !sites.data && (
                    <>
                        <Notice error>{sites.error}</Notice>
                        <button
                            className="button secondary"
                            onClick={sites.retry}
                        >
                            {t("retry")}
                        </button>
                    </>
                )}
                {sites.loading ? (
                    <p role="status">{t("loadingPlants")}</p>
                ) : !sites.data ? null : filtered.length ? (
                    <div className="region-grid">
                        {filtered.map((site) => {
                            const metadata =
                                site.plant?.current_configuration ??
                                site.plant?.submission;
                            return (
                                <button
                                    className="region-card"
                                    key={site.id}
                                    onClick={() => open(site.id)}
                                >
                                    <div className="region-card-top">
                                        <span
                                            className="location-icon"
                                            aria-hidden="true"
                                        >
                                            <PlantIcon />
                                        </span>
                                        <StatusTag
                                            group="RegionStatus"
                                            value={site.status}
                                        />
                                    </div>
                                    <h2>{site.plant?.name ?? site.name}</h2>
                                    <p>
                                        {metadata
                                            ? `${format.significant(metadata.dc_capacity_mw, 4)} MW DC`
                                            : t("completeSetup")}{" "}
                                        {" · "}
                                        {site.timezone}
                                    </p>
                                    <div className="region-card-bottom">
                                        <span>
                                            {site.plant
                                                ? t("openPlant")
                                                : t("completeSetup")}
                                        </span>
                                        <PlantIcon arrow />
                                    </div>
                                </button>
                            );
                        })}
                    </div>
                ) : (
                    <>
                        <Empty
                            title={
                                rows.length ? t("noMatches") : t("firstPlant")
                            }
                        >
                            {rows.length
                                ? t("differentSearch")
                                : t("addDetails")}
                        </Empty>
                        {offerCreate && !rows.length && (
                            <div className="empty-action">
                                <button
                                    className="button"
                                    onClick={() => setCreating(true)}
                                >
                                    {t("addPlant")}
                                </button>
                            </div>
                        )}
                    </>
                )}
                {canManage && (
                    <div className="portfolio-footer">
                        <button
                            className="text-button"
                            onClick={() => router.push(routes.trash(tenant.id))}
                        >
                            {t("recycleBin")}
                        </button>
                    </div>
                )}
            </section>
        </>
    );
}
