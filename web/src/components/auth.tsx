"use client";
import { useTranslations } from "next-intl";

import { useEffect, useRef, useState } from "react";
import { ApiError, request, type Session } from "@/lib/api";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { ActionForm, Field } from "./ui";
import { SocialProviderButtons } from "./social-provider-buttons";
import { LanguageSelect } from "./language-select";
import { clearWorkspaceInvitation } from "./workspace-invitation";

type Mode =
    | "login"
    | "register"
    | "verify"
    | "mfa"
    | "forgot"
    | "reset"
    | "socialSignup";
type AuthMessage =
    | ""
    | "codeSent"
    | "resetSent"
    | "resetDone"
    | "verified"
    | "codeRequested"
    | "passwordChanged";

export function Auth({
    session,
    onSession,
    invitationToken,
}: {
    session: Session;
    invitationToken?: string | null;
    onSession: (session: Session) => void;
}) {
    const t = useTranslations("Auth");
    const invitationLabels = useTranslations("WorkspaceMembers");
    const [mode, setMode] = useState<Mode>("login");
    const [message, setMessage] = useState<AuthMessage>("");
    const social = useTranslations("SocialAuth");
    const callbackErrorRef = useRef<boolean | undefined>(undefined);
    const [socialEmail, setSocialEmail] = useState("");
    const [socialError, setSocialError] = useState(false);
    const [showPassword, setShowPassword] = useState(false);

    async function finish(result: AuthResult) {
        if (result.meta?.is_authenticated) {
            onSession(await request<Session>("/api/session/"));
            return;
        }
        const pending = result.data?.flows?.find((flow) => flow.is_pending)?.id;
        if (pending === "provider_signup") {
            setMode("socialSignup");
            setMessage("");
        } else if (pending === "verify_email") {
            setMode("verify");
            setMessage("codeSent");
        } else if (pending === "mfa_authenticate") {
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
        const callbackError = (callbackErrorRef.current ??=
            callback.searchParams.has("social_callback") &&
            callback.searchParams.has("error"));
        if (callback.searchParams.has("social_callback")) {
            callback.searchParams.delete("social_callback");
            callback.searchParams.delete("error");
            callback.searchParams.delete("error_process");
            window.history.replaceState(
                window.history.state,
                "",
                `${callback.pathname}${callback.search}${callback.hash}`,
            );
        }
        authRequest("auth/session", "GET", session.csrf_token)
            .then((result) => {
                if (!active) return;
                setSocialError(callbackError);
                try {
                    if (
                        window.sessionStorage.getItem(
                            "pyracast.passwordChanged",
                        )
                    ) {
                        window.sessionStorage.removeItem(
                            "pyracast.passwordChanged",
                        );
                        setMessage("passwordChanged");
                    }
                } catch {
                    // Browser privacy settings can disable session storage.
                }
                const pending = result.data?.flows?.find(
                    (flow) => flow.is_pending,
                )?.id;
                const next =
                    pending === "provider_signup"
                        ? "socialSignup"
                        : pending === "verify_email"
                          ? "verify"
                          : pending === "mfa_authenticate"
                            ? "mfa"
                            : pending === "password_reset_by_code"
                              ? "reset"
                              : undefined;
                if (next === "socialSignup") {
                    authRequest<{ email?: { email: string }[] }>(
                        "auth/provider/signup",
                        "GET",
                        session.csrf_token,
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
                        current === "login" ? next : current,
                    );
            })
            .catch(() => {
                if (active) setSocialError(callbackError);
            });
        return () => {
            active = false;
        };
    }, [session.csrf_token]);

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
                <section className="auth-story" aria-label="Pyracast">
                    <div className="brand">
                        pyracast<span className="brand-dot">.</span>
                    </div>
                    <div>
                        <p className="eyebrow">{t("solarIntelligence")}</p>
                        <h2>
                            {t("portfolio")}
                            <br />

                            {t("oneView")}
                        </h2>
                        <p>{t("intro")}</p>
                    </div>
                    <div className="auth-story-footer">
                        {t("builtForEnergy")}
                    </div>
                </section>
                <section
                    className="auth-form-panel"
                    aria-labelledby="auth-title"
                >
                    <LanguageSelect session={session} onSession={onSession} />
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
                    {socialError && (
                        <p className="notice error" role="alert">
                            {social("callbackError")}
                        </p>
                    )}
                    {mode === "socialSignup" && <p>{social("signupHint")}</p>}
                    {(mode === "login" || mode === "register") && (
                        <SocialProviderButtons token={session.csrf_token} />
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
                            if (mode === "forgot") {
                                await authRequest(
                                    "auth/password/request",
                                    "POST",
                                    session.csrf_token,
                                    payload,
                                );
                                setMessage("resetSent");
                                setMode("reset");
                            } else if (mode === "reset") {
                                const result = await authRequest(
                                    "auth/password/reset",
                                    "POST",
                                    session.csrf_token,
                                    {
                                        key: payload.key,
                                        password: payload.password,
                                    },
                                );
                                if (
                                    result.status !== 200 &&
                                    (result.status !== 401 ||
                                        result.errors?.length ||
                                        result.data?.flows?.some(
                                            (flow) =>
                                                flow.id ===
                                                    "password_reset_by_code" &&
                                                flow.is_pending,
                                        ))
                                )
                                    throw new ApiError(
                                        result.errors
                                            ?.map((error) => error.message)
                                            .join(" ") || t("invalidReset"),
                                    );
                                setMode("login");
                                setMessage("resetDone");
                            } else {
                                const path = {
                                    login: "auth/login",
                                    register: "auth/signup",
                                    socialSignup: "auth/provider/signup",
                                    verify: "auth/email/verify",
                                    mfa: "auth/2fa/authenticate",
                                }[mode];
                                const result = await authRequest(
                                    path,
                                    "POST",
                                    session.csrf_token,
                                    payload,
                                );
                                if (
                                    mode === "verify" &&
                                    !result.meta?.is_authenticated &&
                                    !result.errors?.length &&
                                    !result.data?.flows?.some(
                                        (flow) =>
                                            flow.id === "verify_email" &&
                                            flow.is_pending,
                                    )
                                ) {
                                    setMode("login");
                                    setMessage("verified");
                                } else {
                                    await finish(result);
                                }
                            }
                        }}
                    >
                        {(mode === "register" || mode === "socialSignup") &&
                            !invitationToken && (
                                <Field
                                    label={t("company")}
                                    name="company_name"
                                    autoComplete="organization"
                                    maxLength={200}
                                />
                            )}
                        {(mode === "verify" || mode === "reset") && (
                            <Field
                                label={t("emailCode")}
                                name="key"
                                autoComplete="one-time-code"
                            />
                        )}
                        {(mode === "login" ||
                            mode === "register" ||
                            mode === "socialSignup" ||
                            mode === "forgot") && (
                            <Field
                                label={t("email")}
                                name="email"
                                defaultValue={
                                    mode === "socialSignup"
                                        ? socialEmail
                                        : undefined
                                }
                                key={
                                    mode === "socialSignup"
                                        ? socialEmail
                                        : "email"
                                }
                                type="email"
                                autoComplete="email"
                                maxLength={254}
                            />
                        )}
                        {(mode === "login" ||
                            mode === "register" ||
                            mode === "reset") && (
                            <>
                                <Field
                                    label={t("password")}
                                    name="password"
                                    type={showPassword ? "text" : "password"}
                                    autoComplete={
                                        mode === "login"
                                            ? "current-password"
                                            : "new-password"
                                    }
                                    minLength={
                                        mode === "login" ? undefined : 12
                                    }
                                    maxLength={1024}
                                />
                                <button
                                    type="button"
                                    className="text-button password-toggle"
                                    aria-pressed={showPassword}
                                    onClick={() =>
                                        setShowPassword(!showPassword)
                                    }
                                >
                                    {showPassword
                                        ? t("hidePassword")
                                        : t("showPassword")}
                                </button>
                            </>
                        )}
                        {mode === "mfa" && (
                            <Field
                                label={t("authenticatorCode")}
                                name="code"
                                autoComplete="one-time-code"
                            />
                        )}
                    </ActionForm>
                    {mode === "verify" && (
                        <ActionForm
                            submit={t("resendCode")}
                            onSubmit={async () => {
                                const result = await authRequest(
                                    "auth/email/verify/resend",
                                    "POST",
                                    session.csrf_token,
                                    {},
                                );
                                if (result.status !== 200)
                                    throw new ApiError(
                                        t("verificationExpired"),
                                    );
                                setMessage("codeRequested");
                            }}
                        >
                            {null}
                        </ActionForm>
                    )}
                    {invitationToken && (
                        <button
                            type="button"
                            className="text-button"
                            onClick={() => {
                                clearWorkspaceInvitation();
                                window.location.replace("/?workspace=customer");
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
                </section>
            </div>
        </main>
    );
}
