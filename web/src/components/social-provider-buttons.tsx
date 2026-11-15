"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { authRequest } from "@/lib/auth-api";

export type SocialProvider = "google" | "microsoft";
export function useSocialProviders(token: string, provided?: SocialProvider[]) {
    const [providers, setProviders] = useState<SocialProvider[]>([]);
    useEffect(() => {
        if (provided) return;
        let active = true;
        authRequest<{ socialaccount?: { providers?: { id: string }[] } }>(
            "config",
            "GET",
            token,
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
    }, [token, provided]);
    return provided ?? providers;
}

export function SocialProviderButtons({
    token,
    process = "login",
    enabledProviders,
}: {
    token: string;
    process?: "login" | "connect";
    enabledProviders?: SocialProvider[];
}) {
    const t = useTranslations("SocialAuth");
    const providers = useSocialProviders(token, enabledProviders);
    if (!providers.length) return null;
    return (
        <div className="social-providers">
            {providers.map((provider) => (
                <form
                    key={provider}
                    action="/api/auth/browser/v1/auth/provider/redirect"
                    method="post"
                    onSubmit={() => {
                        if (process === "connect") {
                            try {
                                window.sessionStorage.setItem(
                                    "pyracast.socialConnect",
                                    "1",
                                );
                            } catch {
                                // Browser privacy settings can disable session storage.
                            }
                        }
                    }}
                >
                    <input
                        type="hidden"
                        name="csrfmiddlewaretoken"
                        value={token}
                    />
                    <input type="hidden" name="provider" value={provider} />
                    <input type="hidden" name="process" value={process} />
                    <input
                        type="hidden"
                        name="callback_url"
                        value={
                            typeof window === "undefined"
                                ? ""
                                : `${window.location.origin}/app/?social_callback=1`
                        }
                    />
                    <button
                        className={`social-provider ${provider}`}
                        type="submit"
                    >
                        <Image
                            unoptimized
                            src={`/identity/${provider === "google" ? "google.png" : "microsoft.svg"}`}
                            width={20}
                            height={20}
                            alt=""
                        />
                        <span>
                            {t(
                                process === "connect"
                                    ? `connect.${provider}`
                                    : `continue.${provider}`,
                            )}
                        </span>
                    </button>
                </form>
            ))}
            {providers.includes("microsoft") && (
                <p className="muted social-provider-hint">
                    {t("microsoftHint")}
                </p>
            )}
            {process === "login" && (
                <div className="auth-divider">
                    <span>{t("emailDivider")}</span>
                </div>
            )}
        </div>
    );
}
