"use client";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useSessionState } from "@/lib/session-client";
import { RequireUser } from "@/features/session/require-user";
import {
    clearWorkspaceInvitation,
    storedInvitation,
    storeInvitation,
    useStoredInvitation,
} from "./workspace-invitation";
import { AcceptInvitation } from "./accept-invitation";
import {
    InvitationDetails,
    type PreviewState,
    useInvitationPreview,
} from "./invitation-preview";

function fragmentToken() {
    return new URLSearchParams(window.location.hash.slice(1)).get("token");
}

function takeFragmentToken() {
    const token = fragmentToken();
    if (token === null) return;
    if (token) storeInvitation(token);
    else clearWorkspaceInvitation();
    window.history.replaceState(
        window.history.state,
        "",
        `${window.location.pathname}${window.location.search}`,
    );
}

export function InvitationEntry() {
    // The stored token exists only in the browser; the server renders the sign-in check alone.
    return useStoredInvitation() === undefined ? (
        <RequireUser>{null}</RequireUser>
    ) : (
        <InvitationPage />
    );
}

function InvitationPage() {
    const t = useTranslations("WorkspaceMembers");
    // The token the page was opened with, "" when its link carries none. A token cleared later
    // means the page is leaving for the workspaces.
    const [opened] = useState(
        () => fragmentToken() ?? storedInvitation() ?? "",
    );
    const stored = useStoredInvitation();
    const token = opened ? stored : "";
    const account = useSessionState().session?.user?.id;
    const signedIn = account !== undefined;
    const preview = useInvitationPreview(token, account);
    const state: PreviewState = token ? preview : { status: "unavailable" };
    useEffect(takeFragmentToken, []);
    if (token === null || token === undefined)
        return <RequireUser>{null}</RequireUser>;
    return (
        <>
            {!signedIn && (
                <section
                    className="invitation-banner"
                    aria-label={t("invitation")}
                >
                    <InvitationDetails state={state} />
                    {state.status === "ready" && (
                        <p>
                            {t("signInToJoin", {
                                email: state.preview.email,
                            })}
                        </p>
                    )}
                </section>
            )}
            <RequireUser>
                <AcceptInvitation invitationToken={token} preview={state} />
            </RequireUser>
        </>
    );
}
