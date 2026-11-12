"use client";

import { useEffect, useState } from "react";
import { ApiError, request, type Session } from "@/lib/api";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { ActionForm, Field } from "./ui";

type Mode = "login" | "register" | "verify" | "mfa" | "forgot" | "reset";

export function Auth({
    session,
    onSession,
}: {
    session: Session;
    onSession: (session: Session) => void;
}) {
    const [mode, setMode] = useState<Mode>("login");
    const [message, setMessage] = useState("");
    const [showPassword, setShowPassword] = useState(false);

    async function finish(result: AuthResult) {
        if (result.meta?.is_authenticated) {
            onSession(await request<Session>("/api/session/"));
            return;
        }
        const pending = result.data?.flows?.find((flow) => flow.is_pending)?.id;
        if (pending === "verify_email") {
            setMode("verify");
            setMessage("Enter the code sent to your email in this browser.");
        } else if (pending === "mfa_authenticate") {
            setMode("mfa");
            setMessage("");
        } else {
            throw new ApiError(
                result.errors?.map((error) => error.message).join(" ") ||
                    "Unable to sign in. Please check your details and try again.",
            );
        }
    }

    useEffect(() => {
        let active = true;
        authRequest("auth/session", "GET", session.csrf_token)
            .then((result) => {
                if (!active) return;
                const pending = result.data?.flows?.find(
                    (flow) => flow.is_pending,
                )?.id;
                const next =
                    pending === "verify_email"
                        ? "verify"
                        : pending === "mfa_authenticate"
                          ? "mfa"
                          : pending === "password_reset_by_code"
                            ? "reset"
                            : undefined;
                if (next)
                    setMode((current) =>
                        current === "login" ? next : current,
                    );
            })
            .catch(() => {});
        return () => {
            active = false;
        };
    }, [session.csrf_token]);

    const titles: Record<Mode, string> = {
        login: "Welcome back",
        register: "Create your company account",
        verify: "Verify your email",
        mfa: "Two-factor authentication",
        forgot: "Reset your password",
        reset: "Choose a new password",
    };
    return (
        <main className="auth-page">
            <div className="auth-layout">
                <section className="auth-story" aria-label="Pyracast">
                    <div className="brand">
                        pyracast<span className="brand-dot">.</span>
                    </div>
                    <div>
                        <p className="eyebrow">Solar intelligence</p>
                        <h2>
                            Your solar portfolio.
                            <br />
                            One clear view.
                        </h2>
                        <p>
                            Forecasts, production and performance for every
                            plant.
                        </p>
                    </div>
                    <div className="auth-story-footer">
                        Built around your energy.
                    </div>
                </section>
                <section
                    className="auth-form-panel"
                    aria-labelledby="auth-title"
                >
                    <nav className="auth-mode-nav" aria-label="Account access">
                        <button
                            type="button"
                            aria-pressed={mode === "login"}
                            onClick={() => {
                                setMode("login");
                                setMessage("");
                            }}
                        >
                            Sign in
                        </button>
                        <button
                            type="button"
                            aria-pressed={mode === "register"}
                            onClick={() => {
                                setMode("register");
                                setMessage("");
                            }}
                        >
                            Create account
                        </button>
                    </nav>
                    <h1 id="auth-title">{titles[mode]}</h1>
                    {message && <p role="status">{message}</p>}
                    <ActionForm
                        key={mode}
                        submit={
                            {
                                login: "Sign in",
                                register: "Create company account",
                                verify: "Verify email",
                                mfa: "Verify code",
                                forgot: "Send reset code",
                                reset: "Reset password",
                            }[mode]
                        }
                        onSubmit={async (data) => {
                            const payload = Object.fromEntries(data);
                            if (mode === "forgot") {
                                await authRequest(
                                    "auth/password/request",
                                    "POST",
                                    session.csrf_token,
                                    payload,
                                );
                                setMessage(
                                    "If an account exists for this address, a reset code will be sent.",
                                );
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
                                            .join(" ") ||
                                            "This reset code is invalid or expired.",
                                    );
                                setMode("login");
                                setMessage(
                                    "Password reset. Sign in with your new password.",
                                );
                            } else {
                                const path = {
                                    login: "auth/login",
                                    register: "auth/signup",
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
                                    setMessage(
                                        "Email verified. You can sign in now.",
                                    );
                                } else {
                                    await finish(result);
                                }
                            }
                        }}
                    >
                        {mode === "register" && (
                            <Field
                                label="Company name"
                                name="company_name"
                                autoComplete="organization"
                                maxLength={200}
                            />
                        )}
                        {(mode === "verify" || mode === "reset") && (
                            <Field
                                label="Email code"
                                name="key"
                                autoComplete="one-time-code"
                            />
                        )}
                        {(mode === "login" ||
                            mode === "register" ||
                            mode === "forgot") && (
                            <Field
                                label="Email"
                                name="email"
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
                                    label="Password"
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
                                        ? "Hide password"
                                        : "Show password"}
                                </button>
                            </>
                        )}
                        {mode === "mfa" && (
                            <Field
                                label="Authenticator or recovery code"
                                name="code"
                                autoComplete="one-time-code"
                            />
                        )}
                    </ActionForm>
                    {mode === "verify" && (
                        <ActionForm
                            submit="Resend verification code"
                            onSubmit={async () => {
                                const result = await authRequest(
                                    "auth/email/verify/resend",
                                    "POST",
                                    session.csrf_token,
                                    {},
                                );
                                if (result.status !== 200)
                                    throw new ApiError(
                                        "The verification session has expired. Please start again.",
                                    );
                                setMessage("A new code was requested.");
                            }}
                        >
                            {null}
                        </ActionForm>
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
                            Forgot password?
                        </button>
                    )}
                </section>
            </div>
        </main>
    );
}
