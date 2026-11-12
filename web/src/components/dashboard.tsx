"use client";
import { useEffect, useState } from "react";
import { request, type Session, type Tenant } from "@/lib/api";
import {
    ActionForm,
    Empty,
    Field,
    Notice,
    Stat,
    Tag,
    useResource,
    useCollection,
} from "./ui";
import { RegionWorkspace } from "./region-workspace";
import { PlantOnboarding, type PlantSite } from "./plant-onboarding";
import { Auth } from "./auth";
import { PlantIcon } from "./plant-icon";
import { RecycleBin } from "./recycle-bin";

export function Dashboard() {
    const [version, setVersion] = useState(0),
        [session, setSession] = useState<Session>();
    const initial = useResource<Session>("/api/session/", version);
    const current = session ?? initial.data;
    useEffect(() => {
        if (!current) return;
        const refresh = () => {
            if (document.visibilityState === "visible")
                request<Session>("/api/session/")
                    .then(setSession)
                    .catch(() => {});
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
                    <h1>Your workspace</h1>
                    {initial.error ? (
                        <>
                            <Notice error>{initial.error}</Notice>
                            <button
                                className="button"
                                onClick={() => setVersion(version + 1)}
                            >
                                Try again
                            </button>
                        </>
                    ) : (
                        <p role="status">Connecting to your workspace…</p>
                    )}
                </div>
            </main>
        );
    if (!current.user) return <Auth session={current} onSession={setSession} />;
    return (
        <Workspace
            key={current.user.id}
            session={current}
            onSession={setSession}
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
    const tenants = useCollection<Tenant>("/api/tenants/");
    const [selected, setSelected] = useState<number>(),
        [view, setView] = useState<"regions" | "profile" | "trash">("regions"),
        [logoutError, setLogoutError] = useState("");
    const tenant =
        tenants.data?.results.find((row) => row.id === selected) ??
        tenants.data?.results[0];
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
                Skip to content
            </a>
            <aside className="sidebar">
                <Brand />
                <div className="workspace-label">Workspace</div>
                <label className="tenant-select">
                    <span className="sr-only">Workspace</span>
                    <select
                        value={tenant?.id ?? ""}
                        onChange={(event) =>
                            setSelected(Number(event.target.value))
                        }
                    >
                        {!tenant && <option value="">No workspaces</option>}
                        {tenants.data?.results.map((row) => (
                            <option key={row.id} value={row.id}>
                                {row.name}
                            </option>
                        ))}
                    </select>
                </label>
                <nav aria-label="Main navigation">
                    <button
                        className={view === "regions" ? "active" : ""}
                        aria-current={view === "regions" ? "page" : undefined}
                        onClick={() => setView("regions")}
                    >
                        <span aria-hidden="true">▦</span> Overview
                    </button>
                    <button
                        className={view === "trash" ? "active" : ""}
                        aria-current={view === "trash" ? "page" : undefined}
                        onClick={() => setView("trash")}
                    >
                        Recycle bin
                    </button>
                    <button
                        className={view === "profile" ? "active" : ""}
                        aria-current={view === "profile" ? "page" : undefined}
                        onClick={() => setView("profile")}
                    >
                        <span aria-hidden="true">⚙</span> Account settings
                    </button>
                </nav>
                <div className="user-box">
                    <div className="avatar">
                        {session.user?.username.slice(0, 1).toUpperCase()}
                    </div>
                    <div>
                        <strong>{session.user?.username}</strong>
                    </div>
                    <button
                        title="Sign out"
                        aria-label="Sign out"
                        onClick={async () => {
                            try {
                                onSession(
                                    await request<Session>(
                                        "/api/session/logout/",
                                        "POST",
                                        {},
                                        session.csrf_token,
                                    ),
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
                        Workspace <span className="separator">/</span>{" "}
                        <strong>
                            {view === "regions"
                                ? "Overview"
                                : view === "trash"
                                  ? "Recycle bin"
                                  : "Account settings"}
                        </strong>
                    </span>
                    <span className="topbar-label">{tenant?.name}</span>
                </header>
                <main id="main" tabIndex={-1}>
                    {logoutError && <Notice error>{logoutError}</Notice>}
                    {tenants.error && <Notice error>{tenants.error}</Notice>}
                    {view === "profile" ? (
                        <>
                            <div className="page-heading">
                                <div>
                                    <h1>Account settings</h1>
                                </div>
                            </div>
                            <section className="panel narrow">
                                <h2>Email address</h2>
                                <p>Default recipient for reminders.</p>
                                <ActionForm
                                    submit="Save email address"
                                    onSubmit={async (data) =>
                                        onSession(
                                            await request<Session>(
                                                "/api/session/",
                                                "PATCH",
                                                { email: data.get("email") },
                                                session.csrf_token,
                                            ),
                                        )
                                    }
                                >
                                    <Field
                                        label="Default email address"
                                        name="email"
                                        type="email"
                                        defaultValue={session.user?.email}
                                    />
                                </ActionForm>
                            </section>
                        </>
                    ) : tenant && view === "trash" ? (
                        <RecycleBin
                            key={tenant.id}
                            tenant={tenant.id}
                            token={session.csrf_token}
                        />
                    ) : tenant ? (
                        <Regions
                            key={tenant.id}
                            tenant={tenant}
                            token={session.csrf_token}
                        />
                    ) : tenants.loading ? (
                        <p role="status">Loading your workspaces…</p>
                    ) : (
                        <Empty title="No workspace yet">
                            Contact your administrator for access.
                        </Empty>
                    )}
                </main>
            </div>
        </div>
    );
}
function Regions({ tenant, token }: { tenant: Tenant; token: string }) {
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
                        Unable to refresh plants. Showing saved details.{" "}
                        {sites.error}
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
                .toLowerCase()
                .includes(search.toLowerCase()),
    );
    return (
        <>
            <div className="page-heading">
                <div>
                    <p className="eyebrow">{tenant.name}</p>
                    <h1>Your solar portfolio</h1>
                    <p>
                        Forecasts, production and operating history for every
                        plant.
                    </p>
                </div>
                <button
                    className="button"
                    onClick={() => setCreating(!creating)}
                >
                    {creating ? "Close form" : "+ Add a plant"}
                </button>
            </div>
            <div className="stats">
                <Stat
                    label="Plants"
                    value={
                        sites.data
                            ? rows.filter((site) => site.plant).length
                            : undefined
                    }
                />
                <Stat
                    label="Approved"
                    value={
                        sites.data
                            ? rows.filter(
                                  (site) =>
                                      site.plant && site.status === "approved",
                              ).length
                            : undefined
                    }
                />
                <Stat
                    label="Awaiting approval"
                    value={
                        sites.data
                            ? rows.filter(
                                  (site) =>
                                      site.plant && site.status === "pending",
                              ).length
                            : undefined
                    }
                />
            </div>
            {creating && (
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
            <section className="panel">
                <div className="section-heading">
                    <h2>
                        Plants{" "}
                        <span className="count" aria-hidden="true">
                            {rows.length}
                        </span>
                    </h2>
                </div>
                {rows.length > 0 && (
                    <div className="portfolio-toolbar">
                        <label className="field">
                            Search plants
                            <input
                                type="search"
                                value={search}
                                onChange={(event) =>
                                    setSearch(event.target.value)
                                }
                                placeholder="Plant name or timezone"
                            />
                        </label>
                        <label className="field">
                            Approval status
                            <select
                                value={status}
                                onChange={(event) =>
                                    setStatus(event.target.value)
                                }
                            >
                                <option value="all">All statuses</option>
                                <option value="pending">
                                    Awaiting approval
                                </option>
                                <option value="approved">Approved</option>
                                <option value="rejected">Rejected</option>
                                <option value="suspended">Suspended</option>
                            </select>
                        </label>
                    </div>
                )}
                {sites.loading ? (
                    <p role="status">Loading plants...</p>
                ) : sites.error ? (
                    <>
                        <Notice error>{sites.error}</Notice>
                        <button
                            className="button secondary"
                            onClick={() => setVersion((value) => value + 1)}
                        >
                            Try again
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
                                    <h3>{site.plant?.name ?? site.name}</h3>
                                    <p>
                                        {metadata
                                            ? `${metadata.dc_capacity_mw} MW DC`
                                            : "Complete setup"}{" "}
                                        {" · "}
                                        {site.timezone}
                                    </p>
                                    <div className="region-card-bottom">
                                        <span>
                                            {site.plant
                                                ? "Open plant"
                                                : "Complete setup"}
                                        </span>
                                        <PlantIcon arrow />
                                    </div>
                                </button>
                            );
                        })}
                    </div>
                ) : (
                    <Empty
                        title={
                            rows.length
                                ? "No matching plants"
                                : "Start with your first plant"
                        }
                    >
                        {rows.length
                            ? "Try a different name or approval status."
                            : "Add its location and technical details in one request."}
                    </Empty>
                )}
            </section>
        </>
    );
}
