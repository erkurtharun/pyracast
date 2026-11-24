"use client";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { api } from "@/lib/api/client";
import { ApiError, readFailure } from "@/lib/api/errors";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { useErrorMessage } from "@/ui/form";
import { Notice, ReadFailed } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";

type Preview = components["schemas"]["InvitationPreviewOutput"];

export type PreviewState =
    | { status: "loading" }
    | { status: "ready"; preview: Preview }
    | { status: "unavailable" }
    | { status: "failed"; error: unknown; retry: () => void };

// The answer depends on the signed-in account: the account that accepted still sees its invitation.
export function useInvitationPreview(
    token: string | null | undefined,
    account: number | undefined,
): PreviewState {
    const [attempt, setAttempt] = useState(0);
    const request = `${account ?? ""}:${token ?? ""}:${attempt}`;
    const [answer, setAnswer] = useState<{
        request: string;
        state: PreviewState;
    }>();
    useEffect(() => {
        if (!token) return;
        let active = true;
        api.post("/api/invitations/preview/", { body: { token } }).then(
            (preview) => {
                if (active)
                    setAnswer({ request, state: { status: "ready", preview } });
            },
            (failure: unknown) => {
                if (!active) return;
                setAnswer({
                    request,
                    state:
                        failure instanceof ApiError &&
                        failure.code === "invitation_unavailable"
                            ? { status: "unavailable" }
                            : {
                                  status: "failed",
                                  error: readFailure(failure),
                                  retry: () => setAttempt((count) => count + 1),
                              },
                });
            },
        );
        return () => {
            active = false;
        };
    }, [token, request]);
    return answer?.request === request ? answer.state : { status: "loading" };
}

export function InvitationDetails({ state }: { state: PreviewState }) {
    const t = useTranslations("WorkspaceMembers");
    const format = useFormat();
    const label = useLabel();
    const errorMessage = useErrorMessage();
    if (state.status === "loading")
        return <p role="status">{t("loadingInvitation")}</p>;
    if (state.status === "unavailable")
        return <Notice error>{t("invitationUnavailable")}</Notice>;
    if (state.status === "failed")
        return (
            <ReadFailed
                resource={{
                    error: errorMessage(state.error),
                    retry: state.retry,
                }}
            />
        );
    const { preview } = state;
    return (
        <div className="invitation-details">
            <p>
                <strong>
                    {t("invitedTo", { workspace: preview.workspace })}
                </strong>
            </p>
            <p>
                {t("invitedAs", {
                    role: label("WorkspaceRole", preview.role),
                })}
                {preview.inviter &&
                    ` · ${t("invitedBy", { inviter: preview.inviter })}`}
            </p>
            <p className="field-hint">
                {t("invitedAddress", {
                    email: preview.email,
                    date: format.deviceTime(preview.expires_at),
                })}
            </p>
        </div>
    );
}
