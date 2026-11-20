"use client";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { appQuery, routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { RequireUser } from "./require-user";
import { storedInvitation } from "@/features/workspace/workspace-invitation";

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

const consoleRoot = "/admin/";

// The server validates the return address too; this check keeps a crafted link on this
// origin and inside the console after the browser has normalised it.
function plain(text: string) {
    return [...text].every(
        (character) =>
            character !== "\\" && character >= " " && character !== "\u007f",
    );
}

function consoleDestination(next: string | null) {
    if (!next || !plain(next) || !URL.canParse(next, window.location.origin))
        return consoleRoot;
    const target = new URL(next, window.location.origin);
    return target.origin === window.location.origin &&
        target.pathname.startsWith(consoleRoot)
        ? `${target.pathname}${target.search}`
        : consoleRoot;
}

function destination(canAdmin: boolean) {
    const { search } = window.location;
    const callback = appQuery(new URLSearchParams(search)).socialCallback;
    if (callback !== "none" && takeSocialConnect(callback === "failed"))
        return { href: `${routes.account()}${search}`, document: true };
    if (storedInvitation()) return { href: routes.invitation() };
    if (canAdmin)
        return {
            href: consoleDestination(url.searchParams.get("next")),
            document: true,
        };
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
