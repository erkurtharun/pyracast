"use client";
import { useLocale, useTranslations } from "next-intl";

import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api/errors";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { loadSession, signOut, useSessionState } from "@/lib/session-client";
import { isLanguage } from "@/i18n/language";
import { publicPath } from "@/lib/public-metadata";
import { addressWithout, appQuery, callbackParams, routes } from "@/lib/routes";
import { ActionForm } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { SocialProviderButtons } from "./social-provider-buttons";
import { LanguageSelect } from "@/ui/language-select";
import { clearWorkspaceInvitation } from "@/features/workspace/workspace-invitation";
import { AuthFields } from "./steps/auth-fields";
import { AuthStory } from "./steps/auth-story";
import {
    isPending,
    modeForPendingFlow,
    takePasswordChanged,
    type AuthMessage,
    type Mode,
    type Refusal,
} from "./steps/modes";
import { ResendCode } from "./steps/resend-code";
import { LeaveStage, RefusalStage, StageGuide } from "./steps/stage";
import { submitAuth } from "./steps/submit";

type ProviderCompletion = "signedIn" | "pending" | "refused";

// The provider's redirect back only verified the login; this request signs in. An
// authenticated answer changed the server's session, so the session store follows it even
// after the sign-in screen has gone.
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
    const security = useTranslations("Security");
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
    const [address, setAddress] = useState("");
    const [refusal, setRefusal] = useState<Refusal | null>(null);
    const headingRef = useRef<HTMLHeadingElement>(null);
    const pending = isPending(mode);

    useEffect(() => {
        if (refusal) headingRef.current?.focus();
    }, [refusal]);

    function enter(next: Mode, notice: AuthMessage = "") {
        setMode(next);
        setMessage(notice);
        setRefusal(null);
    }

    async function leave() {
        await signOut();
        setAddress("");
        enter("login");
    }

    async function finish(result: AuthResult) {
        if (result.meta?.is_authenticated) {
            await loadSession();
            return;
        }
        const next = modeForPendingFlow(result);
        if (next === "socialSignup" || next === "mfa") {
            enter(next);
        } else if (next === "verify") {
            enter("verify");
            setCodeSentAt(Date.now());
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
                if (completed === "signedIn" || !active) return;
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

    const createLabel = invitationToken
        ? t("createAccount")
        : t("createCompany");
    const titles: Record<Mode, string> = {
        login: t("welcome"),
        register: createLabel,
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
                    {!pending && (
                        <nav className="auth-mode-nav" aria-label={t("access")}>
                            <button
                                type="button"
                                aria-current={
                                    mode === "login" ? "page" : undefined
                                }
                                onClick={() => enter("login")}
                            >
                                {t("signIn")}
                            </button>
                            <button
                                type="button"
                                aria-current={
                                    mode === "register" ? "page" : undefined
                                }
                                onClick={() => enter("register")}
                            >
                                {t("createAccount")}
                            </button>
                        </nav>
                    )}
                    <h1 id="auth-title" ref={headingRef} tabIndex={-1}>
                        {pending && refusal
                            ? t(`${refusal}.title`)
                            : titles[mode]}
                    </h1>
                    {ended && <Notice>{security("sessionExpired")}</Notice>}
                    {socialError && (
                        <p className="notice error" role="alert">
                            {social("callbackError")}
                        </p>
                    )}
                    {(mode === "login" || mode === "register") && (
                        <SocialProviderButtons
                            process={mode === "login" ? "login" : "signup"}
                        />
                    )}
                    {message && <p role="status">{t(message)}</p>}
                    {pending && refusal ? (
                        <RefusalStage
                            refusal={refusal}
                            mode={mode}
                            onRetry={() => setRefusal(null)}
                            onLeave={leave}
                        />
                    ) : (
                        <>
                            <StageGuide mode={mode} address={address} />
                            <ActionForm
                                key={mode}
                                permitted
                                success={null}
                                errorTitle={
                                    {
                                        login: t("signInFailed"),
                                        mfa: t("signInFailed"),
                                        register: t("registrationFailed"),
                                        socialSignup: t("registrationFailed"),
                                        verify: t("verificationFailed"),
                                        forgot: t("resetFailed"),
                                        reset: t("resetFailed"),
                                    }[mode]
                                }
                                submit={
                                    {
                                        login: t("signIn"),
                                        register: createLabel,
                                        verify: t("verifyEmail"),
                                        mfa: t("verifyCode"),
                                        forgot: t("sendReset"),
                                        reset: t("resetPassword"),
                                        socialSignup: createLabel,
                                    }[mode]
                                }
                                onSubmit={async (data) => {
                                    const payload = Object.fromEntries(data);
                                    if (
                                        (mode === "register" ||
                                            mode === "socialSignup") &&
                                        invitationToken
                                    )
                                        payload.invitation_token =
                                            invitationToken;
                                    if (typeof payload.email === "string")
                                        setAddress(payload.email);
                                    const step = await submitAuth(
                                        mode,
                                        payload,
                                        t,
                                    );
                                    if ("refusal" in step) {
                                        setMessage("");
                                        setRefusal(step.refusal);
                                        return;
                                    }
                                    if ("result" in step) {
                                        await finish(step.result);
                                        return;
                                    }
                                    enter(step.mode, step.message);
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
                            {pending && (
                                <LeaveStage mode={mode} onLeave={leave} />
                            )}
                        </>
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
                            onClick={() => enter("forgot")}
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
