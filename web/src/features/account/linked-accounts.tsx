"use client";
import { DisplayError, displayMessage } from "@/lib/api/errors";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ApiError } from "@/lib/api/errors";
import {
    authRequest,
    type AuthenticatorRecord,
    type AuthResult,
} from "@/lib/auth-api";
import { addressWithout, appQuery, callbackParams } from "@/lib/routes";
import { Tag } from "@/ui/ui";
import {
    SocialProviderButtons,
    useSocialProviders,
} from "@/features/auth/social-provider-buttons";
import { refreshExpiredSession } from "./security-reauthentication";
import { StepUpPrompt, stepUpMode, useStepUp } from "./step-up";

type ProviderAccount = {
    provider: { id: string; name: string };
    uid: string;
    display: string;
    can_disconnect: boolean;
    sign_in_available: boolean;
};
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
    const [notice, setNotice] = useState<"removed" | "connected">();
    const [busy, setBusy] = useState(false);
    const stepUp = useStepUp();
    const { ask } = stepUp;
    const [canConnect, setCanConnect] = useState(false);
    const allowConnection = useCallback(async () => setCanConnect(true), []);
    const [callbackError, setCallbackError] = useState(
        () =>
            typeof window !== "undefined" &&
            appQuery(new URLSearchParams(window.location.search))
                .socialCallback === "failed",
    );
    const completion = useRef<
        Promise<AuthResult<ProviderAccount[]>> | undefined
    >(undefined);
    useEffect(() => {
        let active = true;
        const url = new URL(window.location.href);
        const outcome = appQuery(url.searchParams).socialCallback;
        if (outcome !== "none")
            window.history.replaceState(
                window.history.state,
                "",
                addressWithout(url, callbackParams),
            );
        // The provider's redirect back only verified the identity; this request connects it.
        if (outcome === "succeeded")
            completion.current = authRequest<ProviderAccount[]>(
                "auth/provider/complete",
                "POST",
            );
        completion.current
            ?.then(async (result) => {
                if (!active) return;
                completion.current = undefined;
                if (result.status === 401) {
                    const mode = stepUpMode(result);
                    if (!mode)
                        await refreshExpiredSession(security("sessionExpired"));
                    else ask(mode, allowConnection);
                    return;
                }
                setVersion((v) => v + 1);
                setNotice("connected");
            })
            .catch((err: unknown) => {
                if (!active) return;
                completion.current = undefined;
                if (err instanceof ApiError && err.status === 400)
                    setError(err.message);
                else setCallbackError(true);
            });
        return () => {
            active = false;
        };
    }, [ask, allowConnection, security]);
    useEffect(() => {
        let active = true;
        authRequest<ProviderAccount[]>("account/providers", "GET")
            .then(async (result) => {
                if (!active) return;
                if (result.status === 401) {
                    if (!result.meta?.is_authenticated)
                        await refreshExpiredSession(security("sessionExpired"));
                    throw new DisplayError(security("confirmIdentityFirst"));
                }
                if (!Array.isArray(result.data))
                    throw new DisplayError(t("loadError"));
                setAccounts(result.data);
            })
            .catch((err) => {
                if (active) setError(displayMessage(err, t("loadError")));
            });
        return () => {
            active = false;
        };
    }, [version, security, t]);
    async function confirmConnection() {
        setBusy(true);
        setError(undefined);
        setNotice(undefined);
        try {
            const result = await authRequest<AuthenticatorRecord[]>(
                "account/authenticators",
                "GET",
            );
            if (result.status === 401)
                await refreshExpiredSession(security("sessionExpired"));
            stepUp.ask(
                result.data?.length
                    ? "mfa"
                    : hasPassword
                      ? "password"
                      : "email",
                allowConnection,
            );
        } catch (err) {
            setError(displayMessage(err, t("loadError")));
        } finally {
            setBusy(false);
        }
    }
    async function disconnect(account: ProviderAccount) {
        setBusy(true);
        setError(undefined);
        setNotice(undefined);
        try {
            const result = await authRequest("account/providers", "DELETE", {
                provider: account.provider.id,
                account: account.uid,
            });
            if (result.status === 401) {
                const mode = stepUpMode(result);
                if (!mode)
                    await refreshExpiredSession(security("sessionExpired"));
                else stepUp.ask(mode, () => disconnect(account));
                return;
            }
            setNotice("removed");
            setVersion((v) => v + 1);
        } catch (err) {
            setError(displayMessage(err, t("loadError")));
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
        !callbackError &&
        !stepUp.pending
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
            {notice && <p role="status">{t(notice)}</p>}
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
                        {!account.sign_in_available && (
                            <Tag>{t("signInUnavailable")}</Tag>
                        )}
                        {!account.can_disconnect && (
                            <span className="fine">
                                {t("lastSignInMethod")}
                            </span>
                        )}
                    </span>
                    <button
                        className="button secondary"
                        disabled={
                            busy || !!stepUp.pending || !account.can_disconnect
                        }
                        onClick={() => void disconnect(account)}
                        aria-label={`${t("disconnect")} ${account.display}`}
                    >
                        {t("disconnect")}
                    </button>
                </div>
            ))}
            <StepUpPrompt
                stepUp={stepUp}
                email={email}
                onError={(message) => setError(message)}
            />
            {canConnect ? (
                <SocialProviderButtons
                    process="connect"
                    enabledProviders={providers}
                />
            ) : (
                !stepUp.pending &&
                providers.length > 0 && (
                    <button
                        className="button secondary"
                        type="button"
                        disabled={busy}
                        onClick={() => void confirmConnection()}
                    >
                        {t("connectAccount")}
                    </button>
                )
            )}
        </section>
    );
}
