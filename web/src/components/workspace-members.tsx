"use client";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { request, type Session, type Tenant } from "@/lib/api";
import { authRequest } from "@/lib/auth-api";
import type { components } from "@/lib/schema";
import { ActionForm, Field, Notice, useResource } from "./ui";
import { clearWorkspaceInvitation } from "./workspace-invitation";

type Member = components["schemas"]["WorkspaceMemberOutput"];
type Invitation = components["schemas"]["InvitationOutput"];

export function WorkspaceMembers({
    tenant,
    token,
}: {
    tenant: Tenant;
    token: string;
}) {
    const t = useTranslations("WorkspaceMembers");
    const [version, setVersion] = useState(0);
    const base = `/api/tenants/${tenant.id}/`;
    const members = useResource<{ members: Member[] }>(
        `${base}members/`,
        version,
    );
    const invitations = useResource<{ invitations: Invitation[] }>(
        `${base}invitations/`,
        version,
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
                <ActionForm
                    submit={t("sendInvitation")}
                    onSubmit={async (data) => {
                        await request(
                            `${base}invitations/`,
                            "POST",
                            { email: data.get("email") },
                            token,
                        );
                        setVersion((value) => value + 1);
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
                <h2>{t("members")}</h2>
                {members.loading && <p role="status">{t("loading")}</p>}
                {members.error && <Notice error>{members.error}</Notice>}
                <ul className="member-list">
                    {members.data?.members.map((member) => (
                        <li key={member.id}>
                            <span>{member.email}</span>
                            <span>{t(member.role)}</span>
                        </li>
                    ))}
                </ul>
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
                                    </span>
                                    <ActionForm
                                        children={null}
                                        submit={t("cancel")}
                                        onSubmit={async () => {
                                            await request(
                                                `${base}invitations/${invitation.id}/`,
                                                "DELETE",
                                                undefined,
                                                token,
                                            );
                                            setVersion((value) => value + 1);
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
    token,
    session,
}: {
    token: string;
    session: Session;
}) {
    const t = useTranslations("WorkspaceMembers");
    return (
        <main className="auth-page">
            <section className="auth-card">
                <h1>{t("joinWorkspace")}</h1>
                <p>{session.user?.email}</p>
                <ActionForm
                    children={null}
                    submit={t("accept")}
                    onSubmit={async () => {
                        await request(
                            "/api/invitations/accept/",
                            "POST",
                            { token },
                            session.csrf_token,
                        );
                        clearWorkspaceInvitation();
                        window.location.replace("/?workspace=customer");
                    }}
                />
                <ActionForm
                    children={null}
                    submit={t("useAnotherAccount")}
                    onSubmit={async () => {
                        await authRequest(
                            "auth/session",
                            "DELETE",
                            session.csrf_token,
                        );
                        window.location.replace("/invitations/accept");
                    }}
                />
                <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                        clearWorkspaceInvitation();
                        window.location.replace("/?workspace=customer");
                    }}
                >
                    {t("dismiss")}
                </button>
            </section>
        </main>
    );
}
