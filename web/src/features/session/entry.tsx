"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useApiList } from "@/lib/api/query";
import { appQuery, routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { storedInvitation } from "@/features/workspace/workspace-invitation";
import { chooseWorkspace } from "@/features/workspace/workspace-choice";
import { LaunchCard } from "./app-root";
import { RequireUser } from "./require-user";

const consoleRoot = "/admin/";
const unprintable = /\|(?! )[\p{C}\p{Z}]/u;

type Target = { href: string; document?: boolean };

// The server validates the return address too; this check keeps a crafted link on this
// origin and inside the console after the browser has normalised it.
function consoleDestination(next: string | null) {
    if (
        !next ||
        next !== next.trim() ||
        unprintable.test(next) ||
        !URL.canParse(next, window.location.origin)
    )
        return consoleRoot;
    const target = new URL(next, window.location.origin);
    return target.origin === window.location.origin &&
        target.pathname.startsWith(consoleRoot)
        ? `${target.pathname}${target.search}`
        : consoleRoot;
}

function destination(canAdmin: boolean): Target | undefined {
    const { search } = window.location;
    const params = new URLSearchParams(search);
    // A signed-in browser completes every provider return on the account page.
    if (appQuery(params).socialCallback !== "none")
        return { href: `${routes.account()}${search}`, document: true };
    if (storedInvitation()) return { href: routes.invitation() };
    if (canAdmin)
        return {
            href: consoleDestination(params.get("next")),
            document: true,
        };
    return undefined;
}

export function Entry() {
    return (
        <RequireUser>
            <EntryResolver />
        </RequireUser>
    );
}

function EntryResolver() {
    const canAdmin = Boolean(useSessionState().session?.user?.can_admin);
    const target = destination(canAdmin);
    return target ? <Navigate {...target} /> : <WorkspaceEntry />;
}

// The shell reads the same list under the same key, so it shows the workspace without reading again.
function WorkspaceEntry() {
    const tenants = useApiList("/api/tenants/", {});
    if (!tenants.data)
        return (
            <LaunchCard
                failure={
                    tenants.error
                        ? { error: tenants.error, retry: tenants.retry }
                        : undefined
                }
            />
        );
    const tenant = chooseWorkspace(tenants.data.results);
    return (
        <Navigate
            href={tenant ? routes.portfolio(tenant.id) : routes.workspaces()}
        />
    );
}

// The router keeps this screen, the launch card, until the navigation commits.
function Navigate({ href, document }: Target) {
    const router = useRouter();
    useEffect(() => {
        if (document) window.location.replace(href);
        else router.replace(href);
    }, [document, href, router]);
    return <LaunchCard />;
}
