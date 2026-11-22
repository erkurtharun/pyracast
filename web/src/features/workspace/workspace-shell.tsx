"use client";
import { useTranslations } from "next-intl";
import { usePathname, useRouter } from "next/navigation";
import {
    createContext,
    Suspense,
    useContext,
    useEffect,
    useTransition,
    type ReactNode,
} from "react";
import type { Tenant } from "@/lib/api";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { contactEmail } from "@/lib/public-metadata";
import { cadence, useApiList } from "@/lib/api/query";
import { routes, screenOf, type Screen } from "@/lib/routes";
import {
    confirmAnswer,
    signOut,
    useSessionClock,
    useSessionState,
    type SessionUser,
} from "@/lib/session-client";
import { Brand } from "@/ui/brand";
import { LanguageSelect } from "@/ui/language-select";
import { Empty, Notice, RefreshFailed } from "@/ui/ui";
import { WorkspaceAccess } from "@/ui/workspace-access";
import { RecentDeletionProvider } from "@/features/trash/recent-deletion";
import { InstallApp } from "./install-app";

const rememberedKey = "pyracast.workspace";
const Workspace = createContext<{
    tenant?: Tenant;
    user: SessionUser;
    navigating: boolean;
} | null>(null);

function useWorkspaceContext() {
    const value = useContext(Workspace);
    if (!value) throw new Error("A workspace screen needs the shell.");
    return value;
}

export function useWorkspace() {
    const { tenant, user } = useWorkspaceContext();
    if (!tenant) throw new Error("A workspace screen needs a workspace.");
    return { tenant, user };
}

export function useOptionalWorkspace() {
    return useWorkspaceContext().tenant;
}

export function useNavigating() {
    return useWorkspaceContext().navigating;
}

function remembered() {
    try {
        return Number(window.localStorage.getItem(rememberedKey));
    } catch {
        return undefined;
    }
}

function remember(tenant: number) {
    try {
        window.localStorage.setItem(rememberedKey, String(tenant));
    } catch {
        // Browser privacy settings can disable local storage.
    }
}

function switchTarget(screen: Screen, tenant: Tenant) {
    if (screen === "inbox") return routes.inbox(tenant.id);
    if (tenant.role === "admin" && (screen === "members" || screen === "trash"))
        return routes[screen](tenant.id);
    return routes.portfolio(tenant.id);
}

export function WorkspaceShell({
    tenantId,
    children,
}: {
    tenantId?: number;
    children: ReactNode;
}) {
    const t = useTranslations("Dashboard");
    const errors = useTranslations("Errors");
    const router = useRouter();
    const screen = screenOf(usePathname());
    const { session, changed } = useSessionState();
    const user = session?.user;
    if (!user) throw new Error("The workspace shell needs a signed-in user.");
    const tenants = useApiList(
        "/api/tenants/",
        {},
        { poll: cadence.background },
    );
    const { updatedAt } = tenants;
    useEffect(() => {
        if (updatedAt) confirmAnswer(updatedAt);
    }, [updatedAt]);
    const rows = tenants.data?.results ?? [];
    const tenant =
        tenantId === undefined
            ? (rows.find((row) => row.id === remembered()) ?? rows[0])
            : rows.find((row) => row.id === tenantId);
    useEffect(() => {
        if (tenantId !== undefined && tenant) remember(tenant.id);
    }, [tenantId, tenant]);
    const canManage = tenant?.role === "admin";
    const inboxCount =
        (canManage ? (tenant?.pending_review_count ?? 0) : 0) +
        (tenant?.unread_outcome_count ?? 0);
    const [navigating, startNavigation] = useTransition();
    const go = (target: string) => startNavigation(() => router.push(target));
    const entry = (active: boolean) => ({
        className: active ? "active" : "",
        "aria-current": active ? ("page" as const) : undefined,
    });
    const title =
        screen === "trash"
            ? t("recycleBin")
            : screen === "members"
              ? t("members")
              : screen === "inbox"
                ? t("inbox")
                : screen === "account"
                  ? t("accountSettings")
                  : t("overview");
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
                        onChange={(event) => {
                            const next = rows.find(
                                (row) => row.id === Number(event.target.value),
                            );
                            if (next) go(switchTarget(screen, next));
                        }}
                    >
                        {!tenant && (
                            <option value="">{t("noWorkspaces")}</option>
                        )}
                        {rows.map((row) => (
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
                        {...entry(
                            screen === "portfolio" ||
                                screen === "plant" ||
                                screen === "trash",
                        )}
                        onClick={() =>
                            go(
                                tenant
                                    ? routes.portfolio(tenant.id)
                                    : routes.workspaces(),
                            )
                        }
                    >
                        <span aria-hidden="true">▦</span> {t("overview")}
                    </button>
                    {tenant && canManage && (
                        <button
                            {...entry(screen === "members")}
                            onClick={() => go(routes.members(tenant.id))}
                        >
                            {t("members")}
                        </button>
                    )}
                    {tenant && (
                        <button
                            {...entry(screen === "inbox")}
                            aria-label={
                                inboxCount
                                    ? t("inboxCount", { count: inboxCount })
                                    : t("inbox")
                            }
                            onClick={() => go(routes.inbox(tenant.id))}
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
                        {...entry(screen === "account")}
                        onClick={() => go(routes.account())}
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
                        onClick={() => void signOut().catch(() => {})}
                    >
                        ↪
                    </button>
                </div>
                <InstallApp />
            </aside>
            <div className="main-wrap">
                <header className="topbar">
                    <span>
                        {t("workspace")} <span className="separator">/</span>{" "}
                        <strong>{title}</strong>
                    </span>
                    <span className="topbar-label">{tenant?.name}</span>
                </header>
                <main
                    id="main"
                    tabIndex={-1}
                    className={
                        screen === "account" ? "account-settings" : undefined
                    }
                >
                    {session.expires_at && (
                        <SessionEndingSoon expiresAt={session.expires_at} />
                    )}
                    {changed && <Notice>{errors("sessionChanged")}</Notice>}
                    <RefreshFailed resource={tenants} />
                    <WorkspaceAccess value={screen === "account" || canManage}>
                        <Workspace value={{ tenant, user, navigating }}>
                            <RecentDeletionProvider tenant={tenant?.id}>
                                {screen === "account" ? (
                                    children
                                ) : tenants.loading ? (
                                    <p role="status">
                                        {t("loadingWorkspaces")}
                                    </p>
                                ) : !tenants.data ? (
                                    <>
                                        <Notice error>{tenants.error}</Notice>
                                        <button
                                            className="button secondary"
                                            onClick={tenants.retry}
                                        >
                                            {t("retry")}
                                        </button>
                                    </>
                                ) : tenant ? (
                                    <Suspense
                                        fallback={
                                            <p role="status">
                                                {t("loadingScreen")}
                                            </p>
                                        }
                                    >
                                        {children}
                                    </Suspense>
                                ) : tenantId === undefined ? (
                                    <Empty page title={t("noWorkspace")}>
                                        {t("contactAdmin", {
                                            contact: contactEmail,
                                        })}
                                    </Empty>
                                ) : (
                                    <Notice error>
                                        {t("workspaceUnavailable")}
                                    </Notice>
                                )}
                            </RecentDeletionProvider>
                        </Workspace>
                    </WorkspaceAccess>
                </main>
            </div>
        </div>
    );
}

const endingSoonMs = 5 * 60_000;

function SessionEndingSoon({ expiresAt }: { expiresAt: string }) {
    const t = useTranslations("Dashboard");
    const format = useFormat();
    const now = useSessionClock();
    if (now < new Date(expiresAt).getTime() - endingSoonMs) return null;
    return (
        <Notice>
            {t("sessionEndingSoon", {
                time: format.time(expiresAt, deviceZone(), {
                    timeStyle: "short",
                }),
            })}
        </Notice>
    );
}
