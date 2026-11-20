"use client";
import { useTranslations } from "next-intl";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import type { Facts } from "./model";

export function EnabledPanel({
    facts,
    mfaRequired,
    busy,
    onReplace,
    onRemove,
}: {
    facts: Facts;
    mfaRequired: boolean;
    busy: boolean;
    onReplace: () => void;
    onRemove: () => void;
}) {
    const t = useTranslations("Security");
    const format = useFormat();
    // The server sends seconds since the epoch.
    const date = (seconds: number) =>
        format.time(seconds * 1000, deviceZone(), { dateStyle: "medium" });
    const { recovery } = facts;
    return (
        <div className="mfa-subpanel">
            {facts.addedAt !== undefined && (
                <p>
                    {t("authenticatorAdded", { date: date(facts.addedAt) })}{" "}
                    {facts.lastUsedAt
                        ? t("authenticatorLastUsed", {
                              date: date(facts.lastUsedAt),
                          })
                        : t("authenticatorNotUsed")}
                </p>
            )}
            {recovery && (
                <p>
                    {t("recoveryCount", {
                        unused: recovery.unused,
                        total: recovery.total,
                    })}
                </p>
            )}
            {recovery && recovery.unused <= 2 && (
                <div className="notice" role="status">
                    {t("recoveryLowWarning")}
                </div>
            )}
            {mfaRequired && <p className="fine">{t("policyRequiredNote")}</p>}
            <div className="button-group mfa-actions">
                <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={onReplace}
                >
                    {recovery ? t("replaceRecovery") : t("generateRecovery")}
                </button>
                {!mfaRequired && (
                    <button
                        className="text-button"
                        type="button"
                        disabled={busy}
                        onClick={onRemove}
                    >
                        {t("removeAuthenticator")}
                    </button>
                )}
            </div>
        </div>
    );
}
