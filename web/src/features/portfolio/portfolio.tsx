"use client";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { cadence, useApiList, useRefresh } from "@/lib/api/query";
import { routes } from "@/lib/routes";
import { PlantIcon } from "./plant-icon";
import { PlantOnboarding } from "./plant-onboarding";
import { Empty, Notice, RefreshFailed, Tag } from "@/ui/ui";
import { useWorkspace } from "@/features/workspace/workspace-shell";

export function Portfolio() {
    const t = useTranslations("Dashboard");
    const locale = useLocale();
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
                    <p className="eyebrow">{tenant.name}</p>
                    <h1>
                        {t("plants")}{" "}
                        <span className="count" aria-hidden="true">
                            {rows.length}
                        </span>
                    </h1>
                </div>
                {canManage && (
                    <button
                        className="button"
                        onClick={() => setCreating(!creating)}
                    >
                        {creating ? t("closeForm") : t("addPlant")}
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
                        <label className="field">
                            {t("searchPlants")}
                            <input
                                type="search"
                                value={search}
                                onChange={(event) =>
                                    setSearch(event.target.value)
                                }
                                placeholder={t("searchPlaceholder")}
                            />
                        </label>
                        <label className="field">
                            {t("approvalStatus")}
                            <select
                                value={status}
                                onChange={(event) =>
                                    setStatus(event.target.value)
                                }
                            >
                                <option value="all">{t("allStatuses")}</option>
                                <option value="pending">
                                    {t("awaitingApproval")}
                                </option>
                                <option value="approved">
                                    {t("approved")}
                                </option>
                                <option value="rejected">
                                    {t("rejected")}
                                </option>
                                <option value="suspended">
                                    {t("suspended")}
                                </option>
                            </select>
                        </label>
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
                                        <Tag>{site.status}</Tag>
                                    </div>
                                    <h2>{site.plant?.name ?? site.name}</h2>
                                    <p>
                                        {metadata
                                            ? `${metadata.dc_capacity_mw.toLocaleString(locale, { maximumSignificantDigits: 4 })} MW DC`
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
                    <Empty
                        title={rows.length ? t("noMatches") : t("firstPlant")}
                    >
                        {rows.length ? t("differentSearch") : t("addDetails")}
                    </Empty>
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
