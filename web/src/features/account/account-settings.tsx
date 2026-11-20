"use client";
import { useTranslations } from "next-intl";
import { useSessionState } from "@/lib/session-client";
import { AccountSecurity } from "./account-security";
import { EmailChange } from "./email-change";

export function AccountSettings() {
    const t = useTranslations("Dashboard");
    const user = useSessionState().session?.user;
    if (!user) return null;
    return (
        <>
            <div className="page-heading">
                <div>
                    <h1>{t("accountSettings")}</h1>
                </div>
            </div>
            <section className="panel narrow">
                <h2>{t("emailAddress")}</h2>
                <p className="account-email">{user.email}</p>
                <EmailChange email={user.email} />
            </section>
            <AccountSecurity
                key={`${user.id}:${user.mfa_required}`}
                mfaRequired={user.mfa_required}
                hasPassword={user.has_usable_password}
                email={user.email}
            />
        </>
    );
}
