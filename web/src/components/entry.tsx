"use client";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { appQuery, routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { RequireUser } from "./require-user";
import { storedInvitation } from "./workspace-invitation";

const socialConnectKey = "pyracast.socialConnect";

function takeSocialConnect(failed: boolean) {
    try {
        const connecting = window.sessionStorage.getItem(socialConnectKey);
        window.sessionStorage.removeItem(socialConnectKey);
        return Boolean(connecting) || failed;
    } catch {
        return failed;
    }
}

function destination(canAdmin: boolean) {
    const { search } = window.location;
    const callback = appQuery(new URLSearchParams(search)).socialCallback;
    if (callback !== "none" && takeSocialConnect(callback === "failed"))
        return { href: `${routes.account()}${search}`, document: true };
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
