"use client";
import { useTranslations } from "next-intl";
import { Field } from "@/ui/form";
import type { Mode } from "./modes";

export function AuthFields({
    mode,
    invitationToken,
    socialEmail,
    showPassword,
    onShowPassword,
}: {
    mode: Mode;
    invitationToken?: string | null;
    socialEmail: string;
    showPassword: boolean;
    onShowPassword: (shown: boolean) => void;
}) {
    const t = useTranslations("Auth");
    return (
        <>
            {(mode === "register" || mode === "socialSignup") &&
                !invitationToken && (
                    <Field
                        label={t("company")}
                        name="company_name"
                        autoComplete="organization"
                        maxLength={200}
                        required
                    />
                )}
            {(mode === "verify" || mode === "reset") && (
                <Field
                    label={t("emailCode")}
                    name="key"
                    autoComplete="one-time-code"
                    required
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
                        mode === "socialSignup" ? socialEmail : undefined
                    }
                    key={mode === "socialSignup" ? socialEmail : "email"}
                    type="email"
                    autoComplete="email"
                    maxLength={254}
                    required
                />
            )}
            {(mode === "login" || mode === "register" || mode === "reset") && (
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
                        minLength={mode === "login" ? undefined : 15}
                        maxLength={1024}
                        required
                    />
                    <button
                        type="button"
                        className="text-button password-toggle"
                        aria-pressed={showPassword}
                        onClick={() => onShowPassword(!showPassword)}
                    >
                        {showPassword ? t("hidePassword") : t("showPassword")}
                    </button>
                </>
            )}
            {mode === "mfa" && (
                <Field
                    label={t("authenticatorCode")}
                    name="code"
                    autoComplete="one-time-code"
                    required
                />
            )}
        </>
    );
}
