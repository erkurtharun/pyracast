"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { displayMessage } from "@/lib/api/errors";
import { authRequest } from "@/lib/auth-api";
import { useFormat } from "@/lib/format/use-format";
import { loadSession } from "@/lib/session-client";
import { AsyncButton } from "@/ui/form";
import { Tag } from "@/ui/ui";

export type SignedInSession = {
    id: number;
    ip: string;
    user_agent: string;
    created_at: number;
    last_seen_at: number;
    is_current: boolean;
};

type Listing =
    | { status: "loading" }
    | { status: "failed"; message: string }
    | { status: "ready"; sessions: SignedInSession[] };

const BROWSERS: [RegExp, string][] = [
    [/Edg\//, "Edge"],
    [/OPR\//, "Opera"],
    [/Firefox\//, "Firefox"],
    [/Chrome\//, "Chrome"],
    [/Safari\//, "Safari"],
];
const SYSTEMS: [RegExp, string][] = [
    [/Android/, "Android"],
    [/iPhone|iPad/, "iOS"],
    [/Windows/, "Windows"],
    [/Mac OS X/, "macOS"],
    [/Linux/, "Linux"],
];

export function deviceLabel(userAgent: string) {
    const browser = BROWSERS.find(([pattern]) => pattern.test(userAgent))?.[1];
    const system = SYSTEMS.find(([pattern]) => pattern.test(userAgent))?.[1];
    return [browser, system].filter(Boolean).join(" · ");
}

function sessionsOf(data: unknown): SignedInSession[] | undefined {
    if (!Array.isArray(data)) return undefined;
    return [...(data as SignedInSession[])].sort(
        (first, second) =>
            Number(second.is_current) - Number(first.is_current) ||
            second.last_seen_at - first.last_seen_at,
    );
}

export function SignedInDevices() {
    const t = useTranslations("Devices");
    const format = useFormat();
    const [listing, setListing] = useState<Listing>({ status: "loading" });
    const [attempt, setAttempt] = useState(0);

    useEffect(() => {
        let active = true;
        authRequest<SignedInSession[]>("auth/sessions", "GET")
            .then((result) => {
                if (!active) return;
                const sessions = sessionsOf(result.data);
                setListing(
                    sessions
                        ? { status: "ready", sessions }
                        : { status: "failed", message: t("loadFailed") },
                );
            })
            .catch((failure: unknown) => {
                if (active)
                    setListing({
                        status: "failed",
                        message: displayMessage(failure, t("loadFailed")),
                    });
            });
        return () => {
            active = false;
        };
    }, [attempt, t]);

    async function end(ids: number[]) {
        const result = await authRequest<SignedInSession[]>(
            "auth/sessions",
            "DELETE",
            { sessions: ids },
        );
        if (result.status === 401) {
            await loadSession();
            return;
        }
        const sessions = sessionsOf(result.data);
        if (sessions) setListing({ status: "ready", sessions });
    }

    // The server sends seconds since the epoch.
    const time = (seconds: number) => format.deviceTime(seconds * 1000);
    const others =
        listing.status === "ready"
            ? listing.sessions.filter((session) => !session.is_current)
            : [];

    return (
        <section className="panel narrow" aria-labelledby="devices-title">
            <h2 id="devices-title">{t("title")}</h2>
            <p>{t("description")}</p>
            {listing.status === "loading" && (
                <p role="status">{t("loading")}</p>
            )}
            {listing.status === "failed" && (
                <div className="notice error" role="alert">
                    {listing.message}{" "}
                    <button
                        type="button"
                        className="text-button"
                        onClick={() => {
                            setListing({ status: "loading" });
                            setAttempt((value) => value + 1);
                        }}
                    >
                        {t("retry")}
                    </button>
                </div>
            )}
            {listing.status === "ready" && (
                <ul className="device-list">
                    {listing.sessions.map((session) => {
                        const device =
                            deviceLabel(session.user_agent) ||
                            t("unknownDevice");
                        return (
                            <li key={session.id} className="linked-provider">
                                <span className="linked-provider-identity">
                                    <strong>{device}</strong>
                                    {session.is_current && (
                                        <Tag tone="success">
                                            {t("thisDevice")}
                                        </Tag>
                                    )}
                                    <span className="muted">
                                        {t("details", {
                                            ip: session.ip,
                                            seen: time(session.last_seen_at),
                                            since: time(session.created_at),
                                        })}
                                    </span>
                                </span>
                                {!session.is_current && (
                                    <AsyncButton
                                        variant="secondary"
                                        label={t("endOne", { device })}
                                        errorTitle={t("endFailed")}
                                        onClick={() => end([session.id])}
                                    >
                                        {t("end")}
                                    </AsyncButton>
                                )}
                            </li>
                        );
                    })}
                </ul>
            )}
            {others.length > 1 && (
                <AsyncButton
                    variant="secondary"
                    errorTitle={t("endFailed")}
                    onClick={() => end(others.map((session) => session.id))}
                >
                    {t("endOthers")}
                </AsyncButton>
            )}
        </section>
    );
}
