"use client";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useContext, useEffect, useMemo, useState, useTransition } from "react";
import { keys } from "@/lib/api/keys";
import { QueryOwner, useApiList } from "@/lib/api/query";
import { appQuery, routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { storedInvitation } from "@/features/workspace/workspace-invitation";
import { chooseWorkspace } from "@/features/workspace/workspace-choice";
import { LaunchCard } from "./app-root";
import { RequireUser } from "./require-user";

const consoleRoot = "/admin/";
const unprintable = /\\|(?! )[\p{C}\p{Z}]/u;

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

function WorkspaceEntry() {
    const tenants = useApiList("/api/tenants/", {});
    const owner = useContext(QueryOwner) ?? "";
    const list = useMemo(
        () => ({ queryKey: keys.path(owner, "/api/tenants/") }),
        [owner],
    );
    const client = useQueryClient();
    const reading = useIsFetching(list) > 0;
    const settledReads = () => {
        const state = client.getQueryState(
            keys.request(owner, "/api/tenants/"),
        );
        return (state?.dataUpdateCount ?? 0) + (state?.errorUpdateCount ?? 0);
    };
    const [readsAtMount] = useState(settledReads);
    const answered = settledReads() !== readsAtMount;
    const [navigating, startNavigation] = useTransition();
    const router = useRouter();
    const rows = tenants.error ? undefined : tenants.data?.results;
    const { retry } = tenants;
    useEffect(() => {
        if (navigating || reading || client.isFetching(list)) return;
        if (rows) {
            const tenant = chooseWorkspace(rows);
            startNavigation(() =>
                router.replace(
                    tenant ? routes.portfolio(tenant.id) : routes.workspaces(),
                ),
            );
        } else if (!answered) retry();
    }, [answered, client, list, navigating, reading, retry, router, rows]);
    return (
        <LaunchCard
            failure={
                tenants.error && answered && !reading && !navigating
                    ? { error: tenants.error, retry }
                    : undefined
            }
        />
    );
}

function Navigate({ href, document }: Target) {
    const router = useRouter();
    useEffect(() => {
        if (document) window.location.replace(href);
        else router.replace(href);
    }, [document, href, router]);
    return <LaunchCard />;
}
