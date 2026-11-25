"use client";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import type { Tenant } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import type { components } from "@/lib/schema";
import { ConfirmAction } from "@/ui/action";
import { useErrorMessage } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";

type Member = components["schemas"]["WorkspaceMemberOutput"];

type Removal =
    | { status: "idle" }
    | { status: "confirming" | "pending"; member: Member }
    | { status: "success"; email: string }
    | { status: "error"; message: string };

export function MemberList({
    tenant,
    userId,
}: {
    tenant: Tenant;
    userId: number;
}) {
    const t = useTranslations("WorkspaceMembers");
    const refresh = useRefresh();
    const label = useLabel();
    const errorMessage = useErrorMessage();
    const [removal, setRemoval] = useState<Removal>({ status: "idle" });
    const removing = useRef(false);
    const heading = useRef<HTMLHeadingElement>(null);
    const members = useApiQuery("/api/tenants/{tenant_id}/members/", {
        path: { tenant_id: tenant.id },
    });
    async function remove(member: Member) {
        if (removing.current) return;
        removing.current = true;
        setRemoval({ status: "pending", member });
        try {
            await api.delete("/api/tenants/{tenant_id}/members/{user_id}/", {
                path: { tenant_id: tenant.id, user_id: member.id },
            });
            setRemoval({ status: "success", email: member.email });
        } catch (failure) {
            setRemoval({
                status: "error",
                message: errorMessage(failure),
            });
        } finally {
            removing.current = false;
            await refresh.workspace(tenant.id);
            heading.current?.focus();
        }
    }
    return (
        <section className="panel narrow">
            <h2 ref={heading} tabIndex={-1}>
                {t("members")}
            </h2>
            {removal.status === "success" && (
                <Notice>{t("removed", { email: removal.email })}</Notice>
            )}
            {removal.status === "error" && (
                <Notice error>{removal.message}</Notice>
            )}
            <Resource resource={members} loading={t("loading")}>
                {(data) => (
                    <ul className="member-list">
                        {data.members.map((member) => (
                            <li key={member.id} className="workspace-member">
                                <span>
                                    <span>{member.email}</span>
                                    <small className="field-hint">
                                        {label("WorkspaceRole", member.role)}
                                    </small>
                                </span>
                                {tenant.role === "admin" &&
                                    member.role === "viewer" &&
                                    member.id !== userId && (
                                        <button
                                            type="button"
                                            className="text-button member-remove"
                                            disabled={
                                                removal.status === "pending"
                                            }
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
                )}
            </Resource>
            {(removal.status === "confirming" ||
                removal.status === "pending") && (
                <ConfirmAction
                    key={removal.member.id}
                    label={t("confirmRemoval")}
                    cancel={t("keepMember")}
                    confirm={t("remove")}
                    confirming={t("removing")}
                    pending={removal.status === "pending"}
                    onCancel={() => {
                        setRemoval({ status: "idle" });
                        heading.current?.focus();
                    }}
                    onConfirm={() => void remove(removal.member)}
                >
                    <p className="member-removal-question">
                        {t("removeQuestion")}
                    </p>
                    <p>{removal.member.email}</p>
                    <p>{t("removeHelp")}</p>
                </ConfirmAction>
            )}
        </section>
    );
}
