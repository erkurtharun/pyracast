"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { authRequest } from "@/lib/auth-api";
import { routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { rememberProviderReturn } from "./provider-return";

export type SocialProvider = "google" | "microsoft";
export function useSocialProviders(provided?: SocialProvider[]) {
    const [providers, setProviders] = useState<SocialProvider[]>([]);
    useEffect(() => {
        if (provided) return;
        let active = true;
        authRequest<{ socialaccount?: { providers?: { id: string }[] } }>(
            "config",
            "GET",
        )
            .then((result) => {
                if (active)
                    setProviders(
                        (result.data?.socialaccount?.providers ?? [])
                            .map((p) => p.id)
                            .filter(
                                (id): id is SocialProvider =>
                                    id === "google" || id === "microsoft",
                            ),
                    );
            })
            .catch(() => {});
        return () => {
            active = false;
        };
    }, [provided]);
    return provided ?? providers;
}

export function SocialProviderButtons({
    process = "login",
    enabledProviders,
}: {
    process?: "login" | "signup" | "connect";
    enabledProviders?: SocialProvider[];
}) {
    const t = useTranslations("SocialAuth");
    const csrfToken = useSessionState().session?.csrf_token ?? "";
    const providers = useSocialProviders(enabledProviders);
    const flow = process === "connect" ? "connect" : "login";
    const label = (
        { login: "continue", signup: "signUp", connect: "connect" } as const
    )[process];
    // A console link to return to after the sign-in; the server keeps it only when it is a console
    // path of this site.
    const next =
        flow === "login" && typeof window !== "undefined"
            ? new URLSearchParams(window.location.search).get("next")
            : null;
    if (!providers.length) return null;
    return (
        <div className="social-providers">
            {providers.map((provider) => (
                <form
                    key={provider}
                    action="/api/auth/browser/v1/auth/provider/redirect"
                    method="post"
                    onSubmit={
                        process === "login" ? rememberProviderReturn : undefined
                    }
                >
                    <input
                        type="hidden"
                        name="csrfmiddlewaretoken"
                        value={csrfToken}
                    />
                    <input type="hidden" name="provider" value={provider} />
                    <input type="hidden" name="process" value={flow} />
                    {next && <input type="hidden" name="next" value={next} />}
                    <input
                        type="hidden"
                        name="callback_url"
                        value={
                            typeof window === "undefined"
                                ? ""
                                : `${window.location.origin}${routes.socialCallback()}`
                        }
                    />
                    <button
                        className={`social-provider ${provider}`}
                        type="submit"
                    >
                        <span>{t(`${label}.${provider}`)}</span>
                    </button>
                </form>
            ))}
            {providers.includes("microsoft") && (
                <p className="muted social-provider-hint">
                    {t("microsoftHint")}
                </p>
            )}
            {flow === "login" && (
                <div className="auth-divider">
                    <span>{t("emailDivider")}</span>
                </div>
            )}
        </div>
    );
}
