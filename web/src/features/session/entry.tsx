"use client";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import {
    useContext,
    useEffect,
    useMemo,
    useState,
    useSyncExternalStore,
} from "react";
import { keys } from "@/lib/api/keys";
import { QueryOwner, useApiList } from "@/lib/api/query";
import { appQuery, localPath, routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import {
    forgetProviderLanding,
    providerLanding,
} from "@/features/auth/provider-return";
import { storedInvitation } from "@/features/workspace/workspace-invitation";
import { chooseWorkspace } from "@/features/workspace/workspace-choice";
import { LaunchCard } from "./app-root";
import { RequireUser } from "./require-user";

const consoleRoot = "/admin/";

type Target = { href: string; document?: boolean };

function destination(
    canAdmin: boolean,
    landing: string | undefined,
): Target | undefined {
    const { search } = window.location;
    const params = new URLSearchParams(search);
    // A signed-in browser completes every provider return on the account page.
    if (appQuery(params).socialCallback !== "none")
        return { href: `${routes.account()}${search}`, document: true };
    if (storedInvitation()) return { href: routes.invitation() };
    if (landing) return { href: landing };
    if (canAdmin)
        return {
            href:
                localPath(
                    params.get("next") ?? "",
                    consoleRoot,
                    window.location.origin,
                ) ?? consoleRoot,
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
    const [landing] = useState(providerLanding);
    useEffect(forgetProviderLanding, []);
    const target = destination(canAdmin, landing);
    return target ? <Navigate {...target} /> : <WorkspaceEntry />;
}

// Read as an external store: a render of any priority after set() sees it, which a state update does not guarantee.
function createLatch() {
    let set = false;
    const listeners = new Set<() => void>();
    return {
        isSet: () => set,
        set: () => {
            set = true;
            for (const listener of listeners) listener();
        },
        subscribe: (listener: () => void) => {
            listeners.add(listener);
            return () => void listeners.delete(listener);
        },
    };
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
    const [navigation] = useState(createLatch);
    const dispatched = useSyncExternalStore(
        navigation.subscribe,
        navigation.isSet,
        navigation.isSet,
    );
    const router = useRouter();
    const rows = tenants.error ? undefined : tenants.data?.results;
    const { retry } = tenants;
    useEffect(() => {
        if (navigation.isSet() || reading || client.isFetching(list)) return;
        if (rows) {
            navigation.set();
            const tenant = chooseWorkspace(rows);
            router.replace(
                tenant ? routes.portfolio(tenant.id) : routes.workspaces(),
            );
        } else if (!answered) retry();
    }, [answered, client, list, navigation, reading, retry, router, rows]);
    return (
        <LaunchCard
            failure={
                tenants.error && answered && !reading && !dispatched
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
