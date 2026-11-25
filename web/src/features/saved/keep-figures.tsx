"use client";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import {
    removeSavedFiguresHere,
    savedCopyState,
    savedFiguresAvailable,
    subscribeSavedFigures,
    turnOnSavedFigures,
    useSessionState,
    type Session,
} from "@/lib/session-client";
import {
    contextOf,
    registered,
    startRefresh,
    stopRefresh,
    useShownWorkspace,
} from "./saved-figures";

type Shown =
    | { kind: "absent" }
    | { kind: "off" }
    | { kind: "paused" }
    | { kind: "on"; plants: number };

const offerKey = "pyracast.savedOffer";
const absent: Shown = { kind: "absent" };
let shown: Shown = absent;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

function publish(next: Shown) {
    shown = next;
    for (const listener of listeners) listener();
}

function useShown() {
    return useSyncExternalStore(
        subscribe,
        () => shown,
        () => absent,
    );
}

async function readSwitch(session: Session | undefined) {
    if (!(await registered()) || !(await savedFiguresAvailable())) {
        publish(absent);
        return;
    }
    const { state, plants } = await savedCopyState(session);
    publish(
        state === "on"
            ? { kind: "on", plants }
            : state === "unavailable"
              ? absent
              : { kind: state },
    );
}

function useSwitchActions() {
    const client = useQueryClient();
    const { session } = useSessionState();
    const [busy, setBusy] = useState(false);
    const act = (work: () => Promise<unknown>) => {
        setBusy(true);
        void work()
            .then(() => readSwitch(session))
            .finally(() => setBusy(false));
    };
    return {
        busy,
        session,
        turnOn: () =>
            act(async () => {
                const context = contextOf(client, session);
                if ((await turnOnSavedFigures()) && context)
                    startRefresh(context);
            }),
        remove: () =>
            act(async () => {
                stopRefresh();
                await removeSavedFiguresHere();
            }),
    };
}

export function KeepFigures({ tenant }: { tenant?: number }) {
    useShownWorkspace(tenant);
    const t = useTranslations("KeepFigures");
    const value = useShown();
    const actions = useSwitchActions();
    const { session } = actions;
    const read = useCallback(() => void readSwitch(session), [session]);
    useEffect(() => {
        read();
        const onVisible = () => {
            if (document.visibilityState === "visible") read();
        };
        document.addEventListener("visibilitychange", onVisible);
        const unsubscribe = subscribeSavedFigures(read);
        return () => {
            document.removeEventListener("visibilitychange", onVisible);
            unsubscribe();
        };
    }, [read]);
    if (value.kind === "absent") return null;
    return (
        <details
            className="install-app keep-figures"
            onToggle={(event) => {
                if (event.currentTarget.open) read();
            }}
        >
            <summary>{t("title")}</summary>
            <p>{t("statement")}</p>
            {value.kind === "on" ? (
                <>
                    <p>{t("kept", { count: value.plants })}</p>
                    <button
                        type="button"
                        className="text-button"
                        disabled={actions.busy}
                        onClick={actions.remove}
                    >
                        {t("remove")}
                    </button>
                </>
            ) : value.kind === "paused" ? (
                <p>{t("paused")}</p>
            ) : (
                <button
                    type="button"
                    className="text-button"
                    disabled={actions.busy}
                    onClick={actions.turnOn}
                >
                    {t("turnOn")}
                </button>
            )}
        </details>
    );
}

function readOffer() {
    try {
        return window.localStorage.getItem(offerKey) !== null;
    } catch {
        return true;
    }
}

function standalone() {
    return (
        window.matchMedia("(display-mode: standalone)").matches ||
        (navigator as { standalone?: boolean }).standalone === true
    );
}

export function KeepFiguresOffer() {
    const t = useTranslations("KeepFigures");
    const value = useShown();
    const actions = useSwitchActions();
    const [answered, setAnswered] = useState(readOffer);
    const installed = useSyncExternalStore(
        () => () => {},
        standalone,
        () => false,
    );
    if (!installed || answered || value.kind !== "off") return null;
    const answer = () => {
        setAnswered(true);
        try {
            window.localStorage.setItem(offerKey, "1");
        } catch {
            return;
        }
    };
    return (
        <section className="notice keep-figures-offer" aria-label={t("title")}>
            <strong>{t("title")}</strong>
            <p>{t("statement")}</p>
            <div className="button-group">
                <button
                    type="button"
                    className="button"
                    disabled={actions.busy}
                    onClick={() => {
                        answer();
                        actions.turnOn();
                    }}
                >
                    {t("turnOn")}
                </button>
                <button
                    type="button"
                    className="button secondary"
                    onClick={answer}
                >
                    {t("notNow")}
                </button>
            </div>
        </section>
    );
}
