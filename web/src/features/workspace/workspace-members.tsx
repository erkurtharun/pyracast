"use client";
import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import type { Tenant } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { authRequest } from "@/lib/auth-api";
import type { components } from "@/lib/schema";
import { ActionForm, Field, Notice } from "@/ui/ui";
import { clearWorkspaceInvitation } from "./workspace-invitation";

type Member = components["schemas"]["WorkspaceMemberOutput"];

export function WorkspaceMembers({
    tenant,
    userId,
}: {
    tenant: Tenant;
    userId: number;
}) {
    const t = useTranslations("WorkspaceMembers");
    const refresh = useRefresh();
    const [removal, setRemoval] = useState<
        | { status: "idle" }
        | { status: "confirming" | "pending"; member: Member }
        | { status: "success"; email: string }
        | { status: "error"; message: string }
    >({ status: "idle" });
    const removing = useRef(false);
    const membersHeading = useRef<HTMLHeadingElement>(null);
    const scope = { path: { tenant_id: tenant.id } };
    const members = useApiQuery("/api/tenants/{tenant_id}/members/", scope);
    const invitations = useApiQuery(
        "/api/tenants/{tenant_id}/invitations/",
        scope,
    );
    return (
        <>
            <div className="page-heading">
                <div>
                    <p className="eyebrow">{tenant.name}</p>
                    <h1>{t("title")}</h1>
                </div>
            </div>
            <section className="panel narrow">
                <h2>{t("invite")}</h2>
                {!!members.data?.plants_at_recipient_limit && (
                    <Notice>
                        {t("reminderLimitWarning", {
                            count: members.data.plants_at_recipient_limit,
                        })}
                    </Notice>
                )}
                <ActionForm
                    submit={t("sendInvitation")}
                    onSubmit={async (data) => {
                        await api.post(
                            "/api/tenants/{tenant_id}/invitations/",
                            {
                                ...scope,
                                body: {
                                    email: String(data.get("email") ?? ""),
                                },
                            },
                        );
                        await refresh.workspace(tenant.id);
                    }}
                >
                    <Field
                        label={t("email")}
                        name="email"
                        type="email"
                        autoComplete="email"
                        maxLength={254}
                    />
                </ActionForm>
            </section>
            <section className="panel narrow">
                <h2 ref={membersHeading} tabIndex={-1}>
                    {t("members")}
                </h2>
                {members.loading && <p role="status">{t("loading")}</p>}
                {members.error && <Notice error>{members.error}</Notice>}
                {removal.status === "success" && (
                    <Notice>{t("removed", { email: removal.email })}</Notice>
                )}
                {removal.status === "error" && (
                    <Notice error>{removal.message}</Notice>
                )}
                <ul className="member-list">
                    {members.data?.members.map((member) => (
                        <li key={member.id} className="workspace-member">
                            <span>
                                <span>{member.email}</span>
                                <small className="field-hint">
                                    {t(member.role)}
                                </small>
                            </span>
                            {tenant.role === "admin" &&
                                member.role === "viewer" &&
                                member.id !== userId && (
                                    <button
                                        type="button"
                                        className="text-button member-remove"
                                        disabled={removal.status === "pending"}
                                        aria-label={t("removeNamed", {
                                            email: member.email,
                                        })}
                                        onClick={() =>
                                            setRemoval({
                                                status: "confirming",
                                                member,
                                            })
                                        }
                                    >
                                        {t("remove")}
                                    </button>
                                )}
                        </li>
                    ))}
                </ul>
                {(removal.status === "confirming" ||
                    removal.status === "pending") && (
                    <section
                        className="notice"
                        aria-label={t("confirmRemoval")}
                    >
                        <p className="member-removal-question">
                            {t("removeQuestion", {
                                email: removal.member.email,
                            })}
                        </p>
                        <p>{t("removeHelp")}</p>
                        <div className="button-group">
                            <button
                                type="button"
                                className="button secondary"
                                autoFocus
                                disabled={removal.status === "pending"}
                                onClick={() => {
                                    setRemoval({ status: "idle" });
                                    membersHeading.current?.focus();
                                }}
                            >
                                {t("keepMember")}
                            </button>
                            <button
                                type="button"
                                className="button"
                                disabled={removal.status === "pending"}
                                onClick={async () => {
                                    if (removing.current) return;
                                    removing.current = true;
                                    const member = removal.member;
                                    setRemoval({ status: "pending", member });
                                    try {
                                        await api.delete(
                                            "/api/tenants/{tenant_id}/members/{user_id}/",
                                            {
                                                path: {
                                                    tenant_id: tenant.id,
                                                    user_id: member.id,
                                                },
                                            },
                                        );
                                        setRemoval({
                                            status: "success",
                                            email: member.email,
                                        });
                                    } catch (failure) {
                                        setRemoval({
                                            status: "error",
                                            message: (failure as Error).message,
                                        });
                                    } finally {
                                        removing.current = false;
                                        await refresh.workspace(tenant.id);
                                        membersHeading.current?.focus();
                                    }
                                }}
                            >
                                {removal.status === "pending"
                                    ? t("removing")
                                    : t("remove")}
                            </button>
                        </div>
                    </section>
                )}
            </section>
            <section className="panel narrow">
                <h2>{t("pending")}</h2>
                {invitations.loading && <p role="status">{t("loading")}</p>}
                {invitations.error && (
                    <Notice error>{invitations.error}</Notice>
                )}
                {invitations.data &&
                    (invitations.data.invitations.length ? (
                        <ul className="member-list">
                            {invitations.data.invitations.map((invitation) => (
                                <li key={invitation.id}>
                                    <span>
                                        {invitation.email}
                                        {invitation.delivery_status ===
                                            "uncertain" && (
                                            <small className="field-hint">
                                                {t("deliveryUncertain")}
                                            </small>
                                        )}
                                        {invitation.delivery_status ===
                                            "failed" && (
                                            <small className="field-hint">
                                                {t("deliveryFailed")}
                                            </small>
                                        )}
                                    </span>
                                    <ActionForm
                                        children={null}
                                        submit={t("cancel")}
                                        onSubmit={async () => {
                                            await api.delete(
                                                "/api/tenants/{tenant_id}/invitations/{invitation_id}/",
                                                {
                                                    path: {
                                                        tenant_id: tenant.id,
                                                        invitation_id:
                                                            invitation.id,
                                                    },
                                                },
                                            );
                                            await refresh.workspace(tenant.id);
                                        }}
                                    />
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p>{t("noInvitations")}</p>
                    ))}
            </section>
        </>
    );
}

export function AcceptInvitation({
    invitationToken,
}: {
    invitationToken: string;
}) {
    const t = useTranslations("WorkspaceMembers");
    const { session } = useSessionState();
    return (
        <main className="auth-page">
            <section className="auth-card">
                <h1>{t("joinWorkspace")}</h1>
                <p>{session?.user?.email}</p>
                <ActionForm
                    children={null}
                    submit={t("accept")}
                    onSubmit={async () => {
                        await api.post("/api/invitations/accept/", {
                            body: { token: invitationToken },
                        });
                        clearWorkspaceInvitation();
                        window.location.replace(routes.workspaces());
                    }}
                />
                <ActionForm
                    children={null}
                    submit={t("useAnotherAccount")}
                    onSubmit={async () => {
                        await authRequest("auth/session", "DELETE");
                        window.location.replace(routes.invitation());
                    }}
                />
                <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                        clearWorkspaceInvitation();
                        window.location.replace(routes.workspaces());
                    }}
                >
                    {t("dismiss")}
                </button>
            </section>
        </main>
    );
}
