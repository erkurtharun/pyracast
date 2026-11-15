"use client";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { request, type Session, type Tenant } from "@/lib/api";
import {
    ActionForm,
    Empty,
    Field,
    Notice,
    Tag,
    useResource,
    useCollection,
} from "./ui";
import { RegionWorkspace } from "./region-workspace";
import { PlantOnboarding, type PlantSite } from "./plant-onboarding";
import { Auth } from "./auth";
import { authRequest } from "@/lib/auth-api";
import { AccountSecurity } from "./account-security";
import { PlantIcon } from "./plant-icon";
import { RecycleBin } from "./recycle-bin";
import { SessionLanguage } from "./session-language";
import { useWorkspaceInvitation } from "./workspace-invitation";
import { WorkspaceAccess } from "./workspace-access";
import { WorkspaceMembers, AcceptInvitation } from "./workspace-members";
import { LanguageSelect } from "./language-select";

export function Dashboard() {
    const [version, setVersion] = useState(0),
        [session, setSession] = useState<Session>();
    const initial = useResource<Session>("/api/session/", version);
    const current = session ?? initial.data;
    return (
        <SessionLanguage language={current?.language}>
            <DashboardView
                current={current}
                error={initial.error}
                retry={() => setVersion(version + 1)}
                onSession={setSession}
            />
        </SessionLanguage>
    );
}

function DashboardView({
    current,
    error,
    retry,
    onSession,
}: {
    current?: Session;
    error?: string;
    retry: () => void;
    onSession: (session: Session) => void;
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
                request<Session>("/api/session/")
                    .then(onSession)
                    .catch(() => {});
        };
        window.addEventListener("focus", refresh);
        document.addEventListener("visibilitychange", refresh);
        return () => {
            window.removeEventListener("focus", refresh);
            document.removeEventListener("visibilitychange", refresh);
        };
    }, [current, onSession]);
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
                    session={current}
                    onSession={onSession}
                    invitationToken={invitation.token}
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
                <AcceptInvitation token={invitation.token} session={current} />
            </WorkspaceAccess>
        );
    return (
        <Workspace
            key={current.user.id}
            session={current}
            onSession={onSession}
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
function Workspace({
    session,
    onSession,
}: {
    session: Session;
    onSession: (value: Session) => void;
}) {
    const t = useTranslations("Dashboard");
    const searchParams = useSearchParams();
    const [accessVersion, setAccessVersion] = useState(0);
    const tenants = useCollection<Tenant>(
        "/api/tenants/",
        accessVersion,
        30_000,
    );
    const [selected, setSelected] = useState<number>(),
        [view, setView] = useState<"regions" | "profile" | "trash" | "members">(
            searchParams.get("account") === "security" ? "profile" : "regions",
        ),
        [logoutError, setLogoutError] = useState("");
    const tenant =
        tenants.data?.results.find((row) => row.id === selected) ??
        tenants.data?.results[0];
    const canManage = tenant?.role === "admin";
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
                    {session.user?.can_admin && (
                        <a href="/admin/">{t("administration")}</a>
                    )}
                    <button
                        className={
                            view === "regions" || view === "trash"
                                ? "active"
                                : ""
                        }
                        aria-current={
                            view === "regions" || view === "trash"
                                ? "page"
                                : undefined
                        }
                        onClick={() => setView("regions")}
                    >
                        <span aria-hidden="true">▦</span> {t("overview")}
                    </button>
                    {canManage && (
                        <button
                            className={view === "members" ? "active" : ""}
                            aria-current={
                                view === "members" ? "page" : undefined
                            }
                            onClick={() => setView("members")}
                        >
                            {t("members")}
                        </button>
                    )}
                    <button
                        className={view === "profile" ? "active" : ""}
                        aria-current={view === "profile" ? "page" : undefined}
                        onClick={() => setView("profile")}
                    >
                        <span aria-hidden="true">⚙</span> {t("account")}
                    </button>
                </nav>
                <LanguageSelect session={session} onSession={onSession} />
                <div className="user-box">
                    <div className="avatar">
                        {session.user?.username.slice(0, 1).toUpperCase()}
                    </div>
                    <div>
                        <strong>{session.user?.username}</strong>
                    </div>
                    <button
                        title={t("signOut")}
                        aria-label={t("signOut")}
                        onClick={async () => {
                            try {
                                await authRequest(
                                    "auth/session",
                                    "DELETE",
                                    session.csrf_token,
                                );
                                onSession(
                                    await request<Session>("/api/session/"),
                                );
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
                            {view === "regions"
                                ? t("overview")
                                : view === "trash"
                                  ? t("recycleBin")
                                  : view === "members"
                                    ? t("members")
                                    : t("accountSettings")}
                        </strong>
                    </span>
                    <span className="topbar-label">{tenant?.name}</span>
                </header>
                <main
                    id="main"
                    tabIndex={-1}
                    className={
                        view === "profile" ? "account-settings" : undefined
                    }
                >
                    {logoutError && <Notice error>{logoutError}</Notice>}
                    {tenants.error && <Notice error>{tenants.error}</Notice>}
                    <WorkspaceAccess value={view === "profile" || canManage}>
                        {view === "profile" ? (
                            <>
                                <div className="page-heading">
                                    <div>
                                        <h1>{t("accountSettings")}</h1>
                                    </div>
                                </div>
                                <section className="panel narrow">
                                    <h2>{t("emailAddress")}</h2>
                                    <p className="account-email">
                                        {session.user?.email}
                                    </p>
                                    <details className="account-disclosure">
                                        <summary>{t("changeEmail")}</summary>
                                        <p className="fine">
                                            {t("currentEmail", {
                                                email:
                                                    session.user?.email ?? "",
                                            })}
                                        </p>
                                        <ActionForm
                                            submit={t("sendVerification")}
                                            onSubmit={async (data) => {
                                                const result =
                                                    await authRequest(
                                                        "account/email",
                                                        "POST",
                                                        session.csrf_token,
                                                        {
                                                            email: data.get(
                                                                "email",
                                                            ),
                                                        },
                                                    );
                                                if (
                                                    result.status === 401 &&
                                                    !result.meta
                                                        ?.is_authenticated
                                                ) {
                                                    onSession(
                                                        await request<Session>(
                                                            "/api/session/",
                                                        ),
                                                    );
                                                    throw new Error(
                                                        t("sessionExpired"),
                                                    );
                                                }
                                                if (result.status === 401)
                                                    throw new Error(
                                                        t("confirmIdentity"),
                                                    );
                                            }}
                                        >
                                            <Field
                                                label={t("newEmail")}
                                                name="email"
                                                type="email"
                                            />
                                        </ActionForm>
                                        <ActionForm
                                            submit={t("verifyEmail")}
                                            onSubmit={async (data) => {
                                                const result =
                                                    await authRequest(
                                                        "auth/email/verify",
                                                        "POST",
                                                        session.csrf_token,
                                                        {
                                                            key: data.get(
                                                                "key",
                                                            ),
                                                        },
                                                    );
                                                if (result.status === 401) {
                                                    if (
                                                        !result.meta
                                                            ?.is_authenticated
                                                    )
                                                        onSession(
                                                            await request<Session>(
                                                                "/api/session/",
                                                            ),
                                                        );
                                                    throw new Error(
                                                        result.meta
                                                            ?.is_authenticated
                                                            ? t(
                                                                  "confirmIdentity",
                                                              )
                                                            : t(
                                                                  "sessionExpired",
                                                              ),
                                                    );
                                                }
                                                onSession(
                                                    await request<Session>(
                                                        "/api/session/",
                                                    ),
                                                );
                                            }}
                                        >
                                            <Field
                                                label={t("emailCode")}
                                                name="key"
                                                autoComplete="one-time-code"
                                            />
                                        </ActionForm>
                                    </details>
                                </section>
                                <AccountSecurity
                                    key={`${session.user!.id}:${session.user!.mfa_required}`}
                                    token={session.csrf_token}
                                    mfaRequired={session.user!.mfa_required}
                                    hasPassword={
                                        session.user!.has_usable_password
                                    }
                                    email={session.user!.email}
                                    onSession={onSession}
                                />
                            </>
                        ) : tenant && canManage && view === "members" ? (
                            <WorkspaceMembers
                                key={tenant.id}
                                tenant={tenant}
                                token={session.csrf_token}
                                userId={session.user!.id}
                                onAccessChange={() =>
                                    setAccessVersion((value) => value + 1)
                                }
                            />
                        ) : tenant && canManage && view === "trash" ? (
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
                                    token={session.csrf_token}
                                />
                            </>
                        ) : tenant ? (
                            <Regions
                                key={tenant.id}
                                tenant={tenant}
                                token={session.csrf_token}
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
                </main>
            </div>
        </div>
    );
}
function Regions({
    tenant,
    token,
    onTrash,
}: {
    tenant: Tenant;
    token: string;
    onTrash: () => void;
}) {
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
    const sites = useCollection<PlantSite>(
        `/api/tenants/${tenant.id}/sites/`,
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
                    token={token}
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
                    token={token}
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
                {sites.loading ? (
                    <p role="status">{t("loadingPlants")}</p>
                ) : sites.error ? (
                    <>
                        <Notice error>{sites.error}</Notice>
                        <button
                            className="button secondary"
                            onClick={() => setVersion((value) => value + 1)}
                        >
                            {t("retry")}
                        </button>
                    </>
                ) : filtered.length ? (
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
