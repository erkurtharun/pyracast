"use client";

import { useState } from "react";
import { ApiError, request, type Session } from "@/lib/api";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { ActionForm, Field } from "./ui";

export function AccountSecurity({
    token,
    onSession,
}: {
    token: string;
    onSession: (session: Session) => void;
}) {
    const [setup, setSetup] = useState<{
        secret?: string;
        totp_url?: string;
    }>();
    const [codes, setCodes] = useState<string[]>([]);
    const [message, setMessage] = useState("");
    async function call(
        path: string,
        method: string,
        body?: unknown,
    ): Promise<AuthResult> {
        const result = await authRequest(path, method, token, body);
        if (result.status === 401 && !result.meta?.is_authenticated) {
            onSession(await request<Session>("/api/session/"));
            throw new ApiError(
                "Your session has expired. Please sign in again.",
            );
        }
        if (result.status === 401)
            throw new ApiError(
                result.errors?.some(
                    (error) => error.code === "mfa_reauthentication_required",
                )
                    ? "Verify an authenticator before managing administrator recovery codes."
                    : "Please confirm your password below and try again.",
            );
        return result;
    }
    return (
        <>
            <section className="panel narrow">
                <h2>Confirm your identity</h2>
                <p>Confirm your password before sensitive account changes.</p>
                <ActionForm
                    submit="Confirm password"
                    onSubmit={async (data) => {
                        await call(
                            "auth/reauthenticate",
                            "POST",
                            Object.fromEntries(data),
                        );
                        setMessage("Identity confirmed.");
                    }}
                >
                    <Field
                        label="Current password"
                        name="password"
                        type="password"
                        autoComplete="current-password"
                    />
                </ActionForm>
                <ActionForm
                    submit="Confirm authenticator"
                    onSubmit={async (data) => {
                        await call(
                            "auth/2fa/reauthenticate",
                            "POST",
                            Object.fromEntries(data),
                        );
                        setMessage("Authenticator confirmed.");
                    }}
                >
                    <Field
                        label="Authenticator or recovery code"
                        name="code"
                        autoComplete="one-time-code"
                    />
                </ActionForm>
                {message && <p role="status">{message}</p>}
            </section>
            <section className="panel narrow">
                <h2>Change password</h2>
                <ActionForm
                    submit="Change password"
                    onSubmit={async (data) => {
                        const result = await authRequest(
                            "account/password/change",
                            "POST",
                            token,
                            Object.fromEntries(data),
                        );
                        if (result.status === 401) {
                            const session =
                                await request<Session>("/api/session/");
                            if (!session.user) {
                                onSession(session);
                                return;
                            }
                            throw new ApiError(
                                "Please confirm your identity and try again.",
                            );
                        }
                    }}
                >
                    <Field
                        label="Current password"
                        name="current_password"
                        type="password"
                        autoComplete="current-password"
                    />
                    <Field
                        label="New password"
                        name="new_password"
                        type="password"
                        autoComplete="new-password"
                        minLength={12}
                    />
                </ActionForm>
            </section>
            <section className="panel narrow">
                <h2>Two-factor authentication</h2>
                <p>
                    Add the setup key to an authenticator app, then enter its
                    code. Save recovery codes somewhere safe.
                </p>
                <button
                    className="button"
                    type="button"
                    onClick={async () => {
                        try {
                            const result = await call(
                                "account/authenticators/totp",
                                "GET",
                            );
                            setSetup(result.meta);
                            setMessage("");
                        } catch (error) {
                            setMessage((error as Error).message);
                        }
                    }}
                >
                    Set up authenticator
                </button>
                {setup?.secret && (
                    <>
                        <p>
                            Setup key: <code>{setup.secret}</code>
                        </p>
                        <ActionForm
                            submit="Activate authenticator"
                            onSubmit={async (data) => {
                                await call(
                                    "account/authenticators/totp",
                                    "POST",
                                    Object.fromEntries(data),
                                );
                                setSetup(undefined);
                                setMessage(
                                    "Authenticator activated. Save your recovery codes.",
                                );
                            }}
                        >
                            <Field
                                label="Authenticator code"
                                name="code"
                                autoComplete="one-time-code"
                            />
                        </ActionForm>
                    </>
                )}
                <button
                    className="text-button"
                    type="button"
                    onClick={async () => {
                        try {
                            const result = await call(
                                "account/authenticators/recovery-codes",
                                "GET",
                            );
                            setCodes(
                                (result.data?.unused_codes as
                                    string[] | undefined) ?? [],
                            );
                            setMessage(
                                "Recovery codes are displayed only when available.",
                            );
                        } catch (error) {
                            setMessage((error as Error).message);
                        }
                    }}
                >
                    Show recovery codes
                </button>
                <button
                    className="text-button"
                    type="button"
                    onClick={async () => {
                        try {
                            const result = await call(
                                "account/authenticators/recovery-codes",
                                "POST",
                                {},
                            );
                            setCodes(
                                (result.data?.unused_codes as
                                    string[] | undefined) ?? [],
                            );
                            setMessage(
                                "Previous recovery codes no longer work.",
                            );
                        } catch (error) {
                            setMessage((error as Error).message);
                        }
                    }}
                >
                    Replace recovery codes
                </button>
                {codes.length > 0 && (
                    <ul>
                        {codes.map((code) => (
                            <li key={code}>
                                <code>{code}</code>
                            </li>
                        ))}
                    </ul>
                )}
                <button
                    className="text-button"
                    type="button"
                    onClick={async () => {
                        try {
                            await call("account/authenticators/totp", "DELETE");
                            setCodes([]);
                            setMessage("Authenticator removed.");
                        } catch (error) {
                            setMessage((error as Error).message);
                        }
                    }}
                >
                    Remove authenticator
                </button>
            </section>
        </>
    );
}
