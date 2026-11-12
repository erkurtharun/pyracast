"use client";

import { useState } from "react";
import { request, type Session } from "@/lib/api";
import { ActionForm, Field } from "./ui";

export function Auth({
    session,
    onSession,
}: {
    session: Session;
    onSession: (session: Session) => void;
}) {
    const [mode, setMode] = useState<"login" | "register">("login");
    const [showPassword, setShowPassword] = useState(false);
    const registering = mode === "register";
    return (
        <main className="auth-page">
            <div className="auth-layout">
                <section className="auth-story" aria-label="Pyracast">
                    <div className="brand">
                        <svg
                            width="28"
                            height="28"
                            viewBox="0 0 28 28"
                            aria-hidden="true"
                        >
                            <circle
                                cx="14"
                                cy="14"
                                r="5"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="2"
                            />
                            <path
                                d="M14 2v4m0 16v4M2 14h4m16 0h4M5.5 5.5l3 3m11 11 3 3m0-17-3 3m-11 11-3 3"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="2"
                                strokeLinecap="round"
                            />
                        </svg>
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
                            aria-pressed={!registering}
                            onClick={() => setMode("login")}
                        >
                            Sign in
                        </button>
                        <button
                            type="button"
                            aria-pressed={registering}
                            onClick={() => setMode("register")}
                        >
                            Create account
                        </button>
                    </nav>
                    <h1 id="auth-title">
                        {registering
                            ? "Create your company account"
                            : "Welcome back"}
                    </h1>
                    <p>
                        {registering
                            ? "Start with your company, then add your plants."
                            : "Sign in to your workspace."}
                    </p>
                    <ActionForm
                        key={mode}
                        submit={
                            registering ? "Create company account" : "Sign in"
                        }
                        onSubmit={async (data) =>
                            onSession(
                                await request<Session>(
                                    `/api/session/${registering ? "register" : "login"}/`,
                                    "POST",
                                    Object.fromEntries(data),
                                    session.csrf_token,
                                ),
                            )
                        }
                    >
                        {registering && (
                            <Field
                                label="Company name"
                                name="company_name"
                                autoComplete="organization"
                                maxLength={200}
                                placeholder="e.g. Smart Solar Energy"
                            />
                        )}
                        <Field
                            label="Username"
                            name="username"
                            autoComplete="username"
                            maxLength={150}
                        />
                        {registering && (
                            <Field
                                label="Work email"
                                name="email"
                                type="email"
                                autoComplete="email"
                                maxLength={254}
                            />
                        )}
                        <Field
                            label="Password"
                            name="password"
                            type={showPassword ? "text" : "password"}
                            autoComplete={
                                registering
                                    ? "new-password"
                                    : "current-password"
                            }
                            maxLength={1024}
                            minLength={registering ? 8 : undefined}
                            hint={
                                registering
                                    ? "At least 8 characters. Avoid common words and your account details."
                                    : undefined
                            }
                        />
                        <button
                            type="button"
                            className="text-button password-toggle"
                            aria-pressed={showPassword}
                            onClick={() => setShowPassword(!showPassword)}
                        >
                            {showPassword ? "Hide password" : "Show password"}
                        </button>
                    </ActionForm>
                    {registering && (
                        <p className="fine auth-note">
                            Plant locations are reviewed before forecasts begin.
                        </p>
                    )}
                </section>
            </div>
        </main>
    );
}
