"use client";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { RequireUser } from "./require-user";
import { storedInvitation } from "./workspace-invitation";

const socialConnectKey = "pyracast.socialConnect";

function takeSocialConnect(url: URL) {
    try {
        const connecting = window.sessionStorage.getItem(socialConnectKey);
        window.sessionStorage.removeItem(socialConnectKey);
        return Boolean(connecting) || url.searchParams.has("error");
    } catch {
        return url.searchParams.has("error");
    }
}

function destination(canAdmin: boolean) {
    const url = new URL(window.location.href);
    if (url.searchParams.has("social_callback") && takeSocialConnect(url))
        return { href: `${routes.account()}${url.search}`, document: true };
    if (storedInvitation()) return { href: routes.invitation() };
    if (canAdmin) return { href: "/admin/", document: true };
    return { href: routes.workspaces() };
}

export function Entry() {
    return (
        <RequireUser>
            <EntryResolver />
        </RequireUser>
    );
}

function EntryResolver() {
    const t = useTranslations("Dashboard");
    const router = useRouter();
    const canAdmin = Boolean(useSessionState().session?.user?.can_admin);
    useEffect(() => {
        const target = destination(canAdmin);
        if ("document" in target) window.location.replace(target.href);
        else router.replace(target.href);
    }, [canAdmin, router]);
    return (
        <main className="auth-page">
            <p role="status">
                {canAdmin ? t("openingAdmin") : t("loadingWorkspaces")}
            </p>
        </main>
    );
}
