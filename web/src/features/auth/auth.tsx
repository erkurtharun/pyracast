"use client";
import { useLocale, useTranslations } from "next-intl";

import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api/errors";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { loadSession, useSessionState } from "@/lib/session-client";
import { isLanguage } from "@/i18n/language";
import { publicPath } from "@/lib/public-metadata";
import { addressWithout, appQuery, callbackParams, routes } from "@/lib/routes";
import { ActionForm, Notice } from "@/ui/ui";
import { SocialProviderButtons } from "./social-provider-buttons";
import { LanguageSelect } from "@/ui/language-select";
import { clearWorkspaceInvitation } from "@/features/workspace/workspace-invitation";
import { AuthFields } from "./steps/auth-fields";
import { AuthStory } from "./steps/auth-story";
import {
    modeForPendingFlow,
    takePasswordChanged,
    type AuthMessage,
    type Mode,
} from "./steps/modes";
import { ResendCode } from "./steps/resend-code";
import { submitAuth } from "./steps/submit";

type ProviderCompletion = "signedIn" | "pending" | "refused";

// The provider's redirect back only verified the login; this request signs in.
async function completeProviderLogin(): Promise<ProviderCompletion> {
    try {
        const result = await authRequest("auth/provider/complete", "POST");
        if (result.meta?.is_authenticated) {
            await loadSession();
            return "signedIn";
        }
        return result.data?.flows?.some((flow) => flow.is_pending)
            ? "pending"
            : "refused";
    } catch {
        return "refused";
    }
}

export function Auth({
    invitationToken,
    initialMode,
}: {
    invitationToken?: string | null;
    initialMode: "login" | "register";
}) {
    const t = useTranslations("Auth");
    const dashboard = useTranslations("Dashboard");
    const { ended } = useSessionState();
    const locale = useLocale();
    const language = isLanguage(locale) ? locale : "en";
    const invitationLabels = useTranslations("WorkspaceMembers");
    const [mode, setMode] = useState<Mode>(initialMode);
    const [message, setMessage] = useState<AuthMessage>("");
    const social = useTranslations("SocialAuth");
    const callbackErrorRef = useRef<boolean | undefined>(undefined);
    const completionRef = useRef<
        Promise<ProviderCompletion | undefined> | undefined
    >(undefined);
    const [socialEmail, setSocialEmail] = useState("");
    const [socialError, setSocialError] = useState(false);
    const [showPassword, setShowPassword] = useState(false);
    const [codeSentAt, setCodeSentAt] = useState(0);

    async function finish(result: AuthResult) {
        if (result.meta?.is_authenticated) {
            await loadSession();
            return;
        }
        const pending = modeForPendingFlow(result);
        if (pending === "socialSignup") {
            setMode("socialSignup");
            setMessage("");
        } else if (pending === "verify") {
            setMode("verify");
            setMessage("codeSent");
            setCodeSentAt(Date.now());
        } else if (pending === "mfa") {
            setMode("mfa");
            setMessage("");
        } else {
            throw new ApiError(
                result.errors?.map((error) => error.message).join(" ") ||
                    t("unableSignIn"),
            );
        }
    }

    useEffect(() => {
        let active = true;
        const callback = new URL(window.location.href);
        const outcome = appQuery(callback.searchParams).socialCallback;
        const callbackError = (callbackErrorRef.current ??=
            outcome === "failed");
        const completion = (completionRef.current ??=
            outcome === "succeeded"
                ? completeProviderLogin()
                : Promise.resolve(undefined));
        let refused = false;
        if (outcome !== "none")
            window.history.replaceState(
                window.history.state,
                "",
                addressWithout(callback, callbackParams),
            );
        completion
            .then(async (completed) => {
                if (completed === "signedIn") return;
                refused = completed === "refused";
                const result = await authRequest("auth/session", "GET");
                if (!active) return;
                setSocialError(callbackError || refused);
                if (takePasswordChanged()) setMessage("passwordChanged");
                const next = modeForPendingFlow(result);
                if (next === "socialSignup") {
                    authRequest<{ email?: { email: string }[] }>(
                        "auth/provider/signup",
                        "GET",
                    )
                        .then((signup) => {
                            if (active)
                                setSocialEmail(
                                    signup.data?.email?.[0]?.email ?? "",
                                );
                        })
                        .catch(() => {});
                }
                if (next)
                    setMode((current) =>
                        current === "login" || current === "register"
                            ? next
                            : current,
                    );
            })
            .catch(() => {
                if (active) setSocialError(callbackError || refused);
            });
        return () => {
            active = false;
        };
    }, []);

    const titles: Record<Mode, string> = {
        login: t("welcome"),
        register: t("registerTitle"),
        verify: t("verifyTitle"),
        mfa: t("twoFactor"),
        forgot: t("forgotTitle"),
        reset: t("resetTitle"),
        socialSignup: social("completeSignup"),
    };
    return (
        <main className="auth-page">
            <div className="auth-layout">
                <AuthStory />
                <section
                    className="auth-form-panel"
                    aria-labelledby="auth-title"
                >
                    <LanguageSelect />
                    <nav className="auth-mode-nav" aria-label={t("access")}>
                        <button
                            type="button"
                            aria-pressed={mode === "login"}
                            onClick={() => {
                                setMode("login");
                                setMessage("");
                            }}
                        >
                            {t("signIn")}
                        </button>
                        <button
                            type="button"
                            aria-pressed={mode === "register"}
                            onClick={() => {
                                setMode("register");
                                setMessage("");
                            }}
                        >
                            {t("createAccount")}
                        </button>
                    </nav>
                    <h1 id="auth-title">{titles[mode]}</h1>
                    {ended && <Notice>{dashboard("sessionExpired")}</Notice>}
                    {socialError && (
                        <p className="notice error" role="alert">
                            {social("callbackError")}
                        </p>
                    )}
                    {mode === "socialSignup" && <p>{social("signupHint")}</p>}
                    {(mode === "login" || mode === "register") && (
                        <SocialProviderButtons />
                    )}
                    {message && <p role="status">{t(message)}</p>}
                    <ActionForm
                        key={mode}
                        submit={
                            {
                                login: t("signIn"),
                                register: invitationToken
                                    ? t("createAccount")
                                    : t("createCompany"),
                                verify: t("verifyEmail"),
                                mfa: t("verifyCode"),
                                forgot: t("sendReset"),
                                reset: t("resetPassword"),
                                socialSignup: social("completeSignup"),
                            }[mode]
                        }
                        onSubmit={async (data) => {
                            const payload = Object.fromEntries(data);
                            if (
                                (mode === "register" ||
                                    mode === "socialSignup") &&
                                invitationToken
                            )
                                payload.invitation_token = invitationToken;
                            const step = await submitAuth(mode, payload, t);
                            if ("result" in step) {
                                await finish(step.result);
                                return;
                            }
                            setMode(step.mode);
                            setMessage(step.message);
                        }}
                    >
                        <AuthFields
                            mode={mode}
                            invitationToken={invitationToken}
                            socialEmail={socialEmail}
                            showPassword={showPassword}
                            onShowPassword={setShowPassword}
                        />
                    </ActionForm>
                    {mode === "verify" && (
                        <ResendCode
                            key={codeSentAt}
                            sentAt={codeSentAt}
                            onSent={() => {
                                setCodeSentAt(Date.now());
                                setMessage("codeRequested");
                            }}
                        />
                    )}
                    {invitationToken && (
                        <button
                            type="button"
                            className="text-button"
                            onClick={() => {
                                clearWorkspaceInvitation();
                                window.location.replace(routes.workspaces());
                            }}
                        >
                            {invitationLabels("cancel")}
                        </button>
                    )}
                    {mode === "login" && (
                        <button
                            type="button"
                            className="text-button"
                            onClick={() => {
                                setMode("forgot");
                                setMessage("");
                            }}
                        >
                            {t("forgotPassword")}
                        </button>
                    )}
                    <nav
                        aria-label={t("publicLinks")}
                        className="auth-public-links"
                    >
                        <a href={publicPath(language, "home")}>
                            {t("homepage")}
                        </a>{" "}
                        <a href={publicPath(language, "privacy")}>
                            {t("privacy")}
                        </a>{" "}
                        <a href={publicPath(language, "terms")}>{t("terms")}</a>
                    </nav>
                </section>
            </div>
        </main>
    );
}
