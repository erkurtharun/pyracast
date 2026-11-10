"use client";
import { useEffect, useState } from "react";
import {
    request,
    type Session,
    type Tenant,
    type Region,
    type Page,
} from "@/lib/api";
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
                    <h1>
                        Your next day,
                        <br />
                        in clearer light.
                    </h1>
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
    if (!current.user)
        return (
            <main className="auth-page">
                <div className="auth-card">
                    <Brand />
                    <span className="eyebrow">
                        SOLAR INTELLIGENCE, SIMPLIFIED
                    </span>
                    <h1>
                        Your next day,
                        <br />
                        in clearer light.
                    </h1>
                    <p>
                        Sign in to view your regions, explore forecasts and keep
                        your solar plants on track.
                    </p>
                    <ActionForm
                        submit="Sign in →"
                        onSubmit={async (data) =>
                            setSession(
                                await request<Session>(
                                    "/api/session/login/",
                                    "POST",
                                    Object.fromEntries(data),
                                    current.csrf_token,
                                ),
                            )
                        }
                    >
                        <Field label="Username" name="username" />
                        <Field
                            label="Password"
                            name="password"
                            type="password"
                        />
                    </ActionForm>
                    <p className="fine">
                        Need access? Your workspace administrator can create
                        your account.
                    </p>
                </div>
                <div className="auth-art" aria-hidden="true">
                    <div className="sun" />
                    <div className="horizon one" />
                    <div className="horizon two" />
                    <span>
                        More clarity.
                        <br />
                        Better energy decisions.
                    </span>
                </div>
            </main>
        );
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
            <span aria-hidden="true">✳</span> pyracast
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
            <a className="skip-link" href="#main">
                Skip to content
            </a>
            <aside className="sidebar">
                <Brand />
                <div className="workspace-label">YOUR WORKSPACE</div>
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
                        onClick={() => setView("regions")}
                    >
                        <span aria-hidden="true">▦</span> Overview
                    </button>
                    <button
                        className={view === "trash" ? "active" : ""}
                        onClick={() => setView("trash")}
                    >
                        Recycle bin
                    </button>
                    <button
                        className={view === "profile" ? "active" : ""}
                        onClick={() => setView("profile")}
                    >
                        <span aria-hidden="true">⚙</span> Account settings
                    </button>
                </nav>
                <div className="sidebar-note">
                    <span aria-hidden="true">☀</span>
                    <strong>A brighter outlook</strong>
                    <p>
                        Your forecasts are collected from authorized sources.
                        Freshness and coverage travel with every estimate.
                    </p>
                </div>
                <div className="user-box">
                    <div className="avatar">
                        {session.user?.username.slice(0, 1).toUpperCase()}
                    </div>
                    <div>
                        <strong>{session.user?.username}</strong>
                        <span>Workspace member</span>
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
                    <span className="topbar-label">
                        <i /> Solar operations
                    </span>
                </header>
                <main id="main">
                    {logoutError && <Notice error>{logoutError}</Notice>}
                    {tenants.error && <Notice error>{tenants.error}</Notice>}
                    {view === "profile" ? (
                        <>
                            <div className="page-heading">
                                <div>
                                    <span className="eyebrow">
                                        MAKE IT YOURS
                                    </span>
                                    <h1>Account settings</h1>
                                    <p>
                                        Keep your forecast delivery address up
                                        to date.
                                    </p>
                                </div>
                            </div>
                            <section className="panel narrow">
                                <h2>Profile & notifications</h2>
                                <p>
                                    Signed in as{" "}
                                    <strong>{session.user?.username}</strong>.
                                    Regions can use this email as their default
                                    reminder address.
                                </p>
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
                        <Empty title="Your workspace is waiting">
                            Ask your administrator to add you to a tenant to get
                            started.
                        </Empty>
                    )}
                </main>
                <footer>
                    PYRACAST{" "}
                    <span>Solar forecasts. Thoughtfully delivered.</span>
                </footer>
            </div>
        </div>
    );
}
function Regions({ tenant, token }: { tenant: Tenant; token: string }) {
    const [version, setVersion] = useState(0),
        [selected, setSelected] = useState<Region>(),
        [deleted, setDeleted] = useState<number[]>([]),
        [creating, setCreating] = useState(false),
        [more, setMore] = useState<Region[]>([]),
        [pageError, setPageError] = useState(""),
        [next, setNext] = useState<string | null>();
    const regions = useResource<Page<Region>>(
        `/api/tenants/${tenant.id}/regions/`,
        version,
    );
    if (selected)
        return (
            <RegionWorkspace
                key={selected.id}
                region={selected}
                tenant={tenant.id}
                token={token}
                onBack={() => setSelected(undefined)}
                onDeleted={() => {
                    setDeleted((current) => [...current, selected.id]);
                    setVersion((current) => current + 1);
                    setMore([]);
                    setNext(undefined);
                    setSelected(undefined);
                }}
            />
        );
    const rows = [...(regions.data?.results ?? []), ...more].filter(
        (row) => !deleted.includes(row.id),
    );
    return (
        <>
            <div className="page-heading">
                <div>
                    <span className="eyebrow">YOUR ENERGY, AT A GLANCE</span>
                    <h1>A clearer outlook.</h1>
                    <p>
                        Welcome to {tenant.name}. View forecasts and manage your
                        locations.
                    </p>
                </div>
                <button
                    className="button"
                    onClick={() => setCreating(!creating)}
                >
                    {creating ? "Close form" : "+ Request a region"}
                </button>
            </div>
            <div className="stats">
                <Stat
                    label="Regions on this page"
                    value={rows.length}
                    note="Your forecast locations"
                />
                <Stat
                    label="Approved regions"
                    value={
                        rows.filter((row) => row.status === "approved").length
                    }
                    note="Ready for scheduled collection"
                />
                <Stat
                    label="Awaiting approval"
                    value={
                        rows.filter((row) => row.status === "pending").length
                    }
                    note="Reviewed by your administrator"
                />
            </div>
            {creating && (
                <section className="panel">
                    <div className="section-heading">
                        <div>
                            <h2>Request a region</h2>
                            <p>
                                Choose your location. Your administrator will
                                connect and approve the source.
                            </p>
                        </div>
                    </div>
                    <ActionForm
                        submit="Request region"
                        onSubmit={async (data) => {
                            const region = await request<Region>(
                                `/api/tenants/${tenant.id}/regions/`,
                                "POST",
                                {
                                    name: data.get("name"),
                                    latitude: Number(data.get("latitude")),
                                    longitude: Number(data.get("longitude")),
                                    timezone: data.get("timezone"),
                                },
                                token,
                            );
                            setVersion(version + 1);
                            setMore([]);
                            setNext(undefined);
                            setSelected(region);
                        }}
                    >
                        <div className="form-grid">
                            <Field
                                label="Region name"
                                name="name"
                                placeholder="e.g. Antalya solar park"
                            />
                            <Field
                                label="IANA timezone"
                                name="timezone"
                                defaultValue={
                                    Intl.DateTimeFormat().resolvedOptions()
                                        .timeZone
                                }
                                placeholder="Europe/Istanbul"
                            />
                            <Field
                                label="Latitude (°)"
                                name="latitude"
                                type="number"
                                min={-90}
                                max={90}
                                step="any"
                            />
                            <Field
                                label="Longitude (°)"
                                name="longitude"
                                type="number"
                                min={-180}
                                max={180}
                                step="any"
                            />
                        </div>
                    </ActionForm>
                </section>
            )}
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>
                            Your regions{" "}
                            <span className="count">
                                {regions.data?.count ?? "—"}
                            </span>
                        </h2>
                        <p>
                            One place for forecasts, plants and timely updates.
                        </p>
                    </div>
                    <span className="fine">Select a region to explore →</span>
                </div>
                {regions.loading ? (
                    <p role="status">Loading regions…</p>
                ) : regions.error ? (
                    <>
                        <Notice error>{regions.error}</Notice>
                        <button
                            className="button secondary"
                            onClick={() => setVersion(version + 1)}
                        >
                            Try again
                        </button>
                    </>
                ) : rows.length ? (
                    <div className="region-grid">
                        {rows.map((region) => (
                            <button
                                key={region.id}
                                className="region-card"
                                onClick={() => setSelected(region)}
                            >
                                <div className="region-card-top">
                                    <span
                                        className="location-icon"
                                        aria-hidden="true"
                                    >
                                        ⌖
                                    </span>
                                    <Tag>{region.status}</Tag>
                                </div>
                                <h3>{region.name}</h3>
                                <p>
                                    {region.latitude.toFixed(3)}°,{" "}
                                    {region.longitude.toFixed(3)}°
                                </p>
                                <div className="region-card-bottom">
                                    <span>{region.timezone}</span>
                                    <span aria-hidden="true">↗</span>
                                </div>
                            </button>
                        ))}
                    </div>
                ) : (
                    <Empty title="Every forecast starts with a place">
                        Request your first region to bring your solar outlook
                        into focus.
                    </Empty>
                )}
                {(next === undefined ? regions.data?.next : next) && (
                    <button
                        className="button secondary"
                        onClick={async () => {
                            try {
                                const url = new URL(
                                    (next === undefined
                                        ? regions.data?.next
                                        : next)!,
                                    window.location.origin,
                                );
                                const page = await request<Page<Region>>(
                                    url.pathname + url.search,
                                );
                                setMore([...more, ...page.results]);
                                setNext(page.next);
                            } catch (error) {
                                setPageError((error as Error).message);
                            }
                        }}
                    >
                        Load more regions
                    </button>
                )}
                {pageError && <Notice error>{pageError}</Notice>}
            </section>
            <div className="tip-strip">
                <span aria-hidden="true">◷</span>
                <div>
                    <strong>A little ahead of tomorrow.</strong>
                    <p>
                        Open an approved region to choose when you receive fresh
                        forecast emails.
                    </p>
                </div>
            </div>
        </>
    );
}
