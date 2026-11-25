"use client";
import { useTranslations } from "next-intl";
import { useEffect, useState, useSyncExternalStore } from "react";
import {
    readStored,
    removeSavedFiguresHere,
    savedCopyState,
    savedFiguresAvailable,
    subscribeSavedFigures,
    turnOnSavedFigures,
    useSessionState,
    writeStored,
    type Session,
} from "@/lib/session-client";
import { registered, useShownWorkspace } from "./saved-figures";

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
        turnOn: () => act(turnOnSavedFigures),
        remove: () => act(removeSavedFiguresHere),
    };
}

export function KeepFigures({ tenant }: { tenant?: number }) {
    useShownWorkspace(tenant);
    const t = useTranslations("KeepFigures");
    const value = useShown();
    const actions = useSwitchActions();
    const { session } = actions;
    useEffect(() => {
        const read = () => void readSwitch(session);
        read();
        return subscribeSavedFigures(read);
    }, [session]);
    if (value.kind === "absent") return null;
    return (
        <details
            className="install-app keep-figures"
            onToggle={(event) => {
                if (event.currentTarget.open) void readSwitch(session);
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
    const [answeredHere, setAnsweredHere] = useState(false);
    const answered = useSyncExternalStore(
        subscribeSavedFigures,
        () => readStored(offerKey) !== null,
        () => true,
    );
    const installed = useSyncExternalStore(
        () => () => {},
        standalone,
        () => false,
    );
    if (!installed || answered || answeredHere || value.kind !== "off")
        return null;
    const answerOffer = () => {
        setAnsweredHere(true);
        writeStored(offerKey, "1");
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
                        answerOffer();
                        actions.turnOn();
                    }}
                >
                    {t("turnOn")}
                </button>
                <button
                    type="button"
                    className="button secondary"
                    onClick={answerOffer}
                >
                    {t("notNow")}
                </button>
            </div>
        </section>
    );
}
