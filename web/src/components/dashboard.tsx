"use client";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { Tenant } from "@/lib/api";
import { api } from "@/lib/api/client";
import {
    adoptSession,
    loadSession,
    signOut,
    useSessionState,
    type Session,
    type SessionUser,
} from "@/lib/session-client";
import { Empty, Notice, Tag, useCollection } from "./ui";
import { RegionWorkspace } from "./region-workspace";
import { PlantOnboarding, type PlantSite } from "./plant-onboarding";
import { Auth } from "./auth";
import { AccountSecurity } from "./account-security";
import { EmailChange } from "./email-change";
import { PlantIcon } from "./plant-icon";
import { RecycleBin } from "./recycle-bin";
import { SessionLanguage } from "./session-language";
import { useWorkspaceInvitation } from "./workspace-invitation";
import { WorkspaceAccess } from "./workspace-access";
import { WorkspaceMembers, AcceptInvitation } from "./workspace-members";
import { LanguageSelect } from "./language-select";
import { ChangeRequestUpdates } from "./change-request-updates";
import { OperationalInbox } from "./operational-inbox";
import { isLanguage } from "@/i18n/language";

export function Dashboard() {
    const searchParams = useSearchParams();
    const requestedLanguage = searchParams.get("language");
    const languageLabels = useTranslations("Language");
    const [entry, setEntry] = useState<{
        status: "pending" | "failed" | "ready";
    }>({
        status: isLanguage(requestedLanguage) ? "pending" : "ready",
    });
    const [attempt, setAttempt] = useState(0);
    const [failure, setFailure] = useState<string>();
    const { session: current } = useSessionState();
    useEffect(() => {
        let active = true;
        loadSession().catch((error: Error) => {
            if (active) setFailure(error.message);
        });
        return () => {
            active = false;
        };
    }, [attempt]);
    useEffect(() => {
        if (
            !current ||
            !isLanguage(requestedLanguage) ||
            entry.status !== "pending"
        )
            return;
        let active = true;
        const finish = (updated: Session) => {
            if (!active) return;
            const url = new URL(window.location.href);
            url.searchParams.delete("language");
            window.history.replaceState(
                window.history.state,
                "",
                `${url.pathname}${url.search}${url.hash}`,
            );
            adoptSession(updated);
            setEntry({ status: "ready" });
        };
        if (current.user || current.language === requestedLanguage) {
            finish(current);
        } else {
            api.post("/api/session/language/", {
                body: { language: requestedLanguage },
            })
                .then(finish)
                .catch(() => {
                    if (active) setEntry({ status: "failed" });
                });
        }
        return () => {
            active = false;
        };
    }, [current, requestedLanguage, entry.status]);
    return (
        <SessionLanguage language={current?.language}>
            <DashboardView
                current={entry.status === "ready" ? current : undefined}
                error={
                    entry.status === "failed"
                        ? languageLabels("failed")
                        : failure
                }
                retry={() => {
                    if (entry.status === "failed")
                        setEntry({ status: "pending" });
                    else {
                        setFailure(undefined);
                        setAttempt((value) => value + 1);
                    }
                }}
            />
        </SessionLanguage>
    );
}

function DashboardView({
    current,
    error,
    retry,
}: {
    current?: Session;
    error?: string;
    retry: () => void;
}) {
    const t = useTranslations("Dashboard");
    const searchParams = useSearchParams();
    const invitation = useWorkspaceInvitation();
    const enterAdministration =
        current?.user?.can_admin &&
        searchParams.get("workspace") !== "customer" &&
        searchParams.get("account") !== "security" &&
        !searchParams.has("social_callback") &&
        invitation.ready &&
        !invitation.token;
    useEffect(() => {
        if (!current?.user || !searchParams.has("social_callback")) return;
        try {
            const url = new URL(window.location.href);
            if (
                window.sessionStorage.getItem("pyracast.socialConnect") ||
                url.searchParams.has("error")
            ) {
                window.sessionStorage.removeItem("pyracast.socialConnect");
                if (url.searchParams.get("account") !== "security") {
                    url.searchParams.set("account", "security");
                    window.location.replace(`${url.pathname}${url.search}`);
                }
            } else {
                url.searchParams.delete("social_callback");
                url.searchParams.delete("error_process");
                window.history.replaceState(
                    window.history.state,
                    "",
                    `${url.pathname}${url.search}${url.hash}`,
                );
            }
        } catch {
            // Browser privacy settings can disable session storage.
        }
    }, [current, searchParams]);
    useEffect(() => {
        if (enterAdministration) window.location.replace("/admin/");
    }, [enterAdministration]);
    useEffect(() => {
        if (!current) return;
        const refresh = () => {
            if (document.visibilityState === "visible")
                loadSession().catch(() => {});
        };
        window.addEventListener("focus", refresh);
        document.addEventListener("visibilitychange", refresh);
        return () => {
            window.removeEventListener("focus", refresh);
            document.removeEventListener("visibilitychange", refresh);
        };
    }, [current]);
    if (!current)
        return (
            <main className="auth-page">
                <div className="auth-card">
                    <Brand />
                    {error ? (
                        <>
                            <Notice error>{error}</Notice>
                            <button className="button" onClick={retry}>
                                {t("retry")}
                            </button>
                        </>
                    ) : (
                        <div
                            className="loading-indicator"
                            role="progressbar"
                            aria-label="Pyracast"
                        />
                    )}
                </div>
            </main>
        );
    if (!invitation.ready)
        return (
            <main className="auth-page">
                <p role="status">{t("loadingWorkspaces")}</p>
            </main>
        );
    if (!current.user)
        return (
            <WorkspaceAccess value={true}>
                <Auth
                    invitationToken={invitation.token}
                    initialMode={
                        searchParams.get("signup") === "1"
                            ? "register"
                            : "login"
                    }
                />
            </WorkspaceAccess>
        );
    if (enterAdministration)
        return (
            <main className="auth-page">
                <p role="status">{t("openingAdmin")}</p>
            </main>
        );
    if (invitation.token)
        return (
            <WorkspaceAccess value={true}>
                <AcceptInvitation
                    invitationToken={invitation.token}
                    session={current}
                />
            </WorkspaceAccess>
        );
    return (
        <Workspace
            key={current.user.id}
            session={current}
            user={current.user}
        />
    );
}
function Brand() {
    return (
        <div className="brand">
            <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
                <circle
                    cx="14"
                    cy="14"
                    r="5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                />
                <path
                    d="M14 2v4m0 16v4M2 14h4m16 0h4M5.5 5.5l3 3m11 11 3 3m0-17-3 3m-11 11-3 3"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                />
            </svg>{" "}
            pyracast
            <span className="brand-dot">.</span>
        </div>
    );
}
function Workspace({ session, user }: { session: Session; user: SessionUser }) {
    const t = useTranslations("Dashboard");
    const locale = useLocale();
    const searchParams = useSearchParams();
    const [tenantsVersion, setTenantsVersion] = useState(0);
    const tenants = useCollection("/api/tenants/", {}, tenantsVersion, 30_000);
    const endingSoon = useEndingSoon(session.expires_at);
    const [selected, setSelected] = useState<number>(),
        [view, setView] = useState<
            "regions" | "profile" | "trash" | "members" | "inbox"
        >(searchParams.get("account") === "security" ? "profile" : "regions"),
        [logoutError, setLogoutError] = useState("");
    const tenant: Tenant | undefined =
        tenants.data?.results.find((row) => row.id === selected) ??
        tenants.data?.results[0];
    const canManage = tenant?.role === "admin";
    const pendingReviews = canManage ? (tenant?.pending_review_count ?? 0) : 0;
    const unreadOutcomes = tenant?.unread_outcome_count ?? 0;
    const inboxCount = pendingReviews + unreadOutcomes;
    const page =
        view === "profile" ||
        (view === "inbox" && tenant) ||
        ((view === "members" || view === "trash") && canManage)
            ? view
            : "regions";
    const refreshTenants = () => setTenantsVersion((value) => value + 1);
    return (
        <div className="shell">
            <a
                className="skip-link"
                href="#main"
                onClick={(event) => {
                    event.preventDefault();
                    document.getElementById("main")?.focus();
                }}
            >
                {t("skipContent")}
            </a>
            <aside className="sidebar">
                <Brand />
                <div className="workspace-label">{t("workspace")}</div>
                <label className="tenant-select">
                    <span className="sr-only">{t("workspace")}</span>
                    <select
                        value={tenant?.id ?? ""}
                        onChange={(event) =>
                            setSelected(Number(event.target.value))
                        }
                    >
                        {!tenant && (
                            <option value="">{t("noWorkspaces")}</option>
                        )}
                        {tenants.data?.results.map((row) => (
                            <option key={row.id} value={row.id}>
                                {row.name}
                            </option>
                        ))}
                    </select>
                </label>
                {tenant?.role === "viewer" && (
                    <p className="workspace-label workspace-readonly">
                        {t("readOnly")}
                    </p>
                )}
                <nav aria-label={t("navigation")}>
                    {user.can_admin && (
                        <a href="/admin/">{t("administration")}</a>
                    )}
                    <button
                        className={
                            page === "regions" || page === "trash"
                                ? "active"
                                : ""
                        }
                        aria-current={
                            page === "regions" || page === "trash"
                                ? "page"
                                : undefined
                        }
                        onClick={() => setView("regions")}
                    >
                        <span aria-hidden="true">▦</span> {t("overview")}
                    </button>
                    {canManage && (
                        <button
                            className={page === "members" ? "active" : ""}
                            aria-current={
                                page === "members" ? "page" : undefined
                            }
                            onClick={() => setView("members")}
                        >
                            {t("members")}
                        </button>
                    )}
                    {tenant && (
                        <button
                            className={page === "inbox" ? "active" : ""}
                            aria-current={page === "inbox" ? "page" : undefined}
                            aria-label={
                                inboxCount
                                    ? t("inboxCount", { count: inboxCount })
                                    : t("inbox")
                            }
                            onClick={() => setView("inbox")}
                        >
                            {t("inbox")}
                            {inboxCount > 0 && (
                                <strong
                                    className="inbox-badge"
                                    aria-hidden="true"
                                >
                                    {inboxCount}
                                </strong>
                            )}
                        </button>
                    )}
                    <button
                        className={page === "profile" ? "active" : ""}
                        aria-current={page === "profile" ? "page" : undefined}
                        onClick={() => setView("profile")}
                    >
                        <span aria-hidden="true">⚙</span> {t("account")}
                    </button>
                </nav>
                <LanguageSelect />
                <div className="user-box">
                    <div className="avatar">
                        {user.username.slice(0, 1).toUpperCase()}
                    </div>
                    <div>
                        <strong>{user.username}</strong>
                    </div>
                    <button
                        title={t("signOut")}
                        aria-label={t("signOut")}
                        onClick={async () => {
                            try {
                                await signOut();
                            } catch (error) {
                                setLogoutError((error as Error).message);
                            }
                        }}
                    >
                        ↪
                    </button>
                </div>
            </aside>
            <div className="main-wrap">
                <header className="topbar">
                    <span>
                        {t("workspace")} <span className="separator">/</span>{" "}
                        <strong>
                            {page === "regions"
                                ? t("overview")
                                : page === "trash"
                                  ? t("recycleBin")
                                  : page === "members"
                                    ? t("members")
                                    : page === "inbox"
                                      ? t("inbox")
                                      : t("accountSettings")}
                        </strong>
                    </span>
                    <span className="topbar-label">{tenant?.name}</span>
                </header>
                <main
                    id="main"
                    tabIndex={-1}
                    className={
                        page === "profile" ? "account-settings" : undefined
                    }
                >
                    {endingSoon && (
                        <Notice>
                            {t("sessionEndingSoon", {
                                time: new Date(endingSoon).toLocaleTimeString(
                                    locale,
                                    { timeStyle: "short" },
                                ),
                            })}
                        </Notice>
                    )}
                    {logoutError && <Notice error>{logoutError}</Notice>}
                    {tenants.error && <Notice error>{tenants.error}</Notice>}
                    <ChangeRequestUpdates value={refreshTenants}>
                        <WorkspaceAccess
                            value={page === "profile" || canManage}
                        >
                            {page === "profile" ? (
                                <>
                                    <div className="page-heading">
                                        <div>
                                            <h1>{t("accountSettings")}</h1>
                                        </div>
                                    </div>
                                    <section className="panel narrow">
                                        <h2>{t("emailAddress")}</h2>
                                        <p className="account-email">
                                            {user.email}
                                        </p>
                                        <EmailChange
                                            email={user.email}
                                            hasPassword={
                                                user.has_usable_password
                                            }
                                        />
                                    </section>
                                    <AccountSecurity
                                        key={`${user.id}:${user.mfa_required}`}
                                        mfaRequired={user.mfa_required}
                                        hasPassword={user.has_usable_password}
                                        email={user.email}
                                    />
                                </>
                            ) : tenant && page === "inbox" ? (
                                <OperationalInbox
                                    key={`${tenant.id}:${tenant.role}`}
                                    tenant={tenant}
                                />
                            ) : tenant && page === "members" ? (
                                <WorkspaceMembers
                                    key={tenant.id}
                                    tenant={tenant}
                                    userId={user.id}
                                    onAccessChange={refreshTenants}
                                />
                            ) : tenant && page === "trash" ? (
                                <>
                                    <button
                                        className="back-button"
                                        onClick={() => setView("regions")}
                                    >
                                        {t("overview")}
                                    </button>
                                    <RecycleBin
                                        key={tenant.id}
                                        tenant={tenant.id}
                                    />
                                </>
                            ) : tenant ? (
                                <Regions
                                    key={tenant.id}
                                    tenant={tenant}
                                    onTrash={() => setView("trash")}
                                />
                            ) : tenants.loading ? (
                                <p role="status">{t("loadingWorkspaces")}</p>
                            ) : (
                                <Empty title={t("noWorkspace")}>
                                    {t("contactAdmin")}
                                </Empty>
                            )}
                        </WorkspaceAccess>
                    </ChangeRequestUpdates>
                </main>
            </div>
        </div>
    );
}
function Regions({ tenant, onTrash }: { tenant: Tenant; onTrash: () => void }) {
    const t = useTranslations("Dashboard");
    const locale = useLocale();
    const canManage = tenant.role === "admin";
    const [version, setVersion] = useState(0);
    const [selectedId, setSelectedId] = useState<number>();
    const [created, setCreated] = useState<PlantSite>();
    const [deleted, setDeleted] = useState<number[]>([]);
    const [creating, setCreating] = useState(false);
    const [search, setSearch] = useState("");
    const [status, setStatus] = useState("all");
    const sites = useCollection(
        "/api/tenants/{tenant_id}/sites/",
        { path: { tenant_id: tenant.id } },
        version,
        30_000,
    );
    useEffect(() => {
        const sync = () => {
            const match = window.location.hash.match(/^#plant-(\d+)/);
            setSelectedId(match ? Number(match[1]) : undefined);
        };
        sync();
        window.addEventListener("hashchange", sync);
        return () => window.removeEventListener("hashchange", sync);
    }, []);
    const rows = (sites.data?.results ?? []).filter(
        (row) => !deleted.includes(row.id),
    );
    const selected =
        rows.find((row) => row.id === selectedId) ??
        (created?.id === selectedId ? created : undefined);
    const open = (site: PlantSite) => {
        setSelectedId(site.id);
        window.history.pushState(null, "", `#plant-${site.id}/forecast`);
    };
    const back = () => {
        setVersion((value) => value + 1);
        setSelectedId(undefined);
        setCreated(undefined);
        window.history.pushState(null, "", "#plants");
    };
    if (selected)
        return (
            <>
                {sites.error && (
                    <Notice error>
                        {t("refreshFailed")} {sites.error}
                    </Notice>
                )}
                <RegionWorkspace
                    key={selected.id}
                    region={selected}
                    initialPlant={
                        created?.id === selected.id
                            ? (created.plant ?? undefined)
                            : undefined
                    }
                    tenant={tenant.id}
                    onBack={back}
                    onDeleted={() => {
                        setDeleted((current) => [...current, selected.id]);
                        back();
                    }}
                />
            </>
        );
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
                    onCreated={(site) => {
                        setCreated(site);
                        setCreating(false);
                        setVersion((value) => value + 1);
                        open(site);
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
                {sites.error && (
                    <>
                        <Notice error>{sites.error}</Notice>
                        <button
                            className="button secondary"
                            onClick={() => setVersion((value) => value + 1)}
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
                                    onClick={() => open(site)}
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
                        <button className="text-button" onClick={onTrash}>
                            {t("recycleBin")}
                        </button>
                    </div>
                )}
            </section>
        </>
    );
}

function useEndingSoon(expiresAt: string | null) {
    const [endingAt, setEndingAt] = useState<string>();
    useEffect(() => {
        if (!expiresAt) return;
        // Five minutes (300 000 ms) before the absolute session limit.
        const timer = setTimeout(
            () => setEndingAt(expiresAt),
            Math.max(0, new Date(expiresAt).getTime() - Date.now() - 300_000),
        );
        return () => clearTimeout(timer);
    }, [expiresAt]);
    return endingAt;
}
