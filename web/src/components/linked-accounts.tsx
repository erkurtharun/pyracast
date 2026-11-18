"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { authRequest, type AuthenticatorRecord } from "@/lib/auth-api";
import {
    SocialProviderButtons,
    useSocialProviders,
} from "./social-provider-buttons";
import {
    getReauthMode,
    ReauthenticationForm,
    refreshExpiredSession,
    type ReauthMode,
} from "./security-reauthentication";

type ProviderAccount = {
    provider: { id: string; name: string };
    uid: string;
    display: string;
};
type Pending =
    { kind: "connect" } | { kind: "disconnect"; account: ProviderAccount };
export function LinkedAccounts({
    email,
    hasPassword,
}: {
    email: string;
    hasPassword: boolean;
}) {
    const t = useTranslations("SocialAuth");
    const providers = useSocialProviders();
    const security = useTranslations("Security");
    const [accounts, setAccounts] = useState<ProviderAccount[]>();
    const [version, setVersion] = useState(0);
    const [error, setError] = useState<string>();
    const [notice, setNotice] = useState(false);
    const [busy, setBusy] = useState(false);
    const [reauth, setReauth] = useState<{
        mode: ReauthMode;
        pending: Pending;
    }>();
    const [canConnect, setCanConnect] = useState(false);
    const [callbackError] = useState(
        () =>
            typeof window !== "undefined" &&
            new URLSearchParams(window.location.search).has(
                "social_callback",
            ) &&
            new URLSearchParams(window.location.search).has("error"),
    );
    useEffect(() => {
        const url = new URL(window.location.href);
        if (!url.searchParams.has("social_callback")) return;
        url.searchParams.delete("social_callback");
        url.searchParams.delete("error");
        url.searchParams.delete("error_process");
        window.history.replaceState(
            window.history.state,
            "",
            `${url.pathname}${url.search}${url.hash}`,
        );
    }, []);
    useEffect(() => {
        let active = true;
        authRequest<ProviderAccount[]>("account/providers", "GET")
            .then(async (result) => {
                if (!active) return;
                if (result.status === 401) {
                    if (!result.meta?.is_authenticated)
                        await refreshExpiredSession(security("sessionExpired"));
                    throw new Error(security("confirmIdentityFirst"));
                }
                if (!Array.isArray(result.data))
                    throw new Error(t("loadError"));
                setAccounts(result.data);
            })
            .catch((err) => {
                if (active)
                    setError(
                        err instanceof Error ? err.message : t("loadError"),
                    );
            });
        return () => {
            active = false;
        };
    }, [version, security, t]);
    async function confirmConnection() {
        setBusy(true);
        setError(undefined);
        setNotice(false);
        try {
            const result = await authRequest<AuthenticatorRecord[]>(
                "account/authenticators",
                "GET",
            );
            if (result.status === 401)
                await refreshExpiredSession(security("sessionExpired"));
            setReauth({
                mode: result.data?.length
                    ? "mfa"
                    : hasPassword
                      ? "password"
                      : "email",
                pending: { kind: "connect" },
            });
        } catch (err) {
            setError(err instanceof Error ? err.message : t("loadError"));
        } finally {
            setBusy(false);
        }
    }
    async function disconnect(account: ProviderAccount, replay = false) {
        setBusy(true);
        setError(undefined);
        setNotice(false);
        try {
            const result = await authRequest("account/providers", "DELETE", {
                provider: account.provider.id,
                account: account.uid,
            });
            if (result.status === 401) {
                if (!result.meta?.is_authenticated)
                    await refreshExpiredSession(security("sessionExpired"));
                if (replay) throw new Error(security("reauthStillRequired"));
                setReauth({
                    mode: getReauthMode(result, hasPassword),
                    pending: { kind: "disconnect", account },
                });
                return;
            }
            setReauth(undefined);
            setNotice(true);
            setVersion((v) => v + 1);
        } catch (err) {
            setError(err instanceof Error ? err.message : t("loadError"));
        } finally {
            setBusy(false);
        }
    }
    if (
        accounts &&
        !accounts.length &&
        !providers.length &&
        !notice &&
        !error &&
        !callbackError
    )
        return null;
    return (
        <section
            className="panel narrow"
            aria-labelledby="linked-accounts-title"
        >
            <h2 id="linked-accounts-title">{t("linkedAccounts")}</h2>
            {callbackError && (
                <p className="notice error" role="alert">
                    {t("callbackError")}
                </p>
            )}
            {error && (
                <div className="notice error" role="alert">
                    {error}
                    <button
                        className="text-button"
                        onClick={() => {
                            setError(undefined);
                            setVersion((v) => v + 1);
                        }}
                    >
                        {t("retry")}
                    </button>
                </div>
            )}
            {notice && <p role="status">{t("removed")}</p>}
            {accounts?.map((account) => (
                <div
                    className="linked-provider"
                    key={`${account.provider.id}:${account.uid}`}
                >
                    <span className="linked-provider-identity">
                        <strong>
                            {account.provider.id === "google"
                                ? "Google"
                                : account.provider.id === "microsoft"
                                  ? "Microsoft"
                                  : account.provider.name}
                        </strong>
                        <span className="muted">{account.display}</span>
                    </span>
                    <button
                        className="button secondary"
                        disabled={busy || !!reauth}
                        onClick={() => disconnect(account)}
                        aria-label={`${t("disconnect")} ${account.display}`}
                    >
                        {t("disconnect")}
                    </button>
                </div>
            ))}
            {reauth && (
                <ReauthenticationForm
                    key={reauth.mode}
                    mode={reauth.mode}
                    email={email}
                    autoFocus
                    onCancel={() => setReauth(undefined)}
                    onVerified={async () => {
                        if (reauth.pending.kind === "connect") {
                            setCanConnect(true);
                            setReauth(undefined);
                        } else await disconnect(reauth.pending.account, true);
                    }}
                />
            )}
            {canConnect ? (
                <SocialProviderButtons
                    process="connect"
                    enabledProviders={providers}
                />
            ) : (
                !reauth &&
                providers.length > 0 && (
                    <button
                        className="button secondary"
                        type="button"
                        disabled={busy}
                        onClick={confirmConnection}
                    >
                        {t("connectAccount")}
                    </button>
                )
            )}
        </section>
    );
}
