import type { QueryCacheNotifyEvent } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/errors";
import { send } from "@/lib/api/lane";
import { readListOnce } from "@/lib/api/query";
import type { Deadline } from "@/lib/saved/deadline";
import type { DaysAnswer, HeldPlant, Owner, PlantKey } from "@/lib/saved/store";
import { boundedStore, type SavedStore } from "@/lib/session-client";
import {
    shownPlant,
    shownWorkspace,
    subscribeOpened,
    type RefreshContext,
} from "./saved-figures";

const minuteMs = 60_000;
const runEveryMs = 5 * minuteMs;
const attemptGapMs = 5 * minuteMs;
const onScreenAfterMs = 15 * minuteMs;
const otherAfterMs = 60 * minuteMs;
const runLock = "pyracast-saved-refresh";
const productionPath =
    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/";

type Step = "go on" | "stop";

function idOf({ tenant, site, plant }: PlantKey) {
    return `${tenant}/${site}/${plant}`;
}

function samePlant(one: PlantKey, other: PlantKey | undefined) {
    return other !== undefined && idOf(one) === idOf(other);
}

function visible() {
    return document.visibilityState === "visible";
}

function daysUrl({ tenant, site, plant }: PlantKey) {
    return `/api/tenants/${tenant}/regions/${site}/plants/${plant}/days/`;
}

function statusOf(error: unknown) {
    return error instanceof ApiError ? error.status : 0;
}

function productionOf(key: readonly unknown[], plant: PlantKey | undefined) {
    const parameters = key[5] as
        { region_id?: unknown; plant_id?: unknown } | undefined;
    return (
        plant !== undefined &&
        key[3] === plant.tenant &&
        key[4] === productionPath &&
        parameters?.region_id === plant.site &&
        parameters.plant_id === plant.plant
    );
}

function valueOf(data: unknown) {
    return typeof data === "object" && data !== null && "value" in data
        ? data.value
        : undefined;
}

export function mountRefresh(context: RefreshContext): () => void {
    const owner: Owner = { user: context.user, session: context.session };
    const changed = new Set<string>();
    const seen = new Map<string, unknown>();
    let stopped = false;
    let running = false;
    let again = false;

    const call = <T>(
        work: (saved: SavedStore, limit: Deadline) => Promise<T>,
    ) => boundedStore(work);

    const eligible = () => !stopped && visible() && context.current();

    const due = (plants: HeldPlant[], now: number) => {
        const screen = shownPlant();
        const ready = plants.filter((plant) => {
            const { attemptedAt, refreshedAt } = plant;
            if (
                attemptedAt !== null &&
                now >= attemptedAt &&
                now - attemptedAt < attemptGapMs
            )
                return false;
            if (refreshedAt === null) return true;
            const onScreen = samePlant(plant, screen);
            if (onScreen && changed.has(idOf(plant))) return true;
            const age = now - refreshedAt;
            return (
                age < 0 || age >= (onScreen ? onScreenAfterMs : otherAfterMs)
            );
        });
        return ready.sort((one, other) => {
            if (samePlant(one, screen)) return -1;
            if (samePlant(other, screen)) return 1;
            return (other.openedAt ?? -1) - (one.openedAt ?? -1);
        })[0];
    };

    const settle = async (): Promise<Step> => {
        if (!eligible()) return "stop";
        const tenants = await readListOnce(
            context.client,
            context.owner,
            "/api/tenants/",
            {},
        );
        if ("refusal" in tenants) return "stop";
        const workspaces = tenants.value.results.map(({ id, name }) => ({
            tenant: id,
            name,
        }));
        await call((saved, limit) =>
            saved.settle(owner, { workspaces }, limit),
        );
        const tenant = shownWorkspace();
        if (
            tenant === undefined ||
            !workspaces.some((workspace) => workspace.tenant === tenant)
        )
            return "go on";
        if (!eligible()) return "stop";
        const sites = await readListOnce(
            context.client,
            context.owner,
            "/api/tenants/{tenant_id}/sites/",
            { path: { tenant_id: tenant } },
        );
        if ("refusal" in sites) {
            if (sites.refusal.status !== 403 && sites.refusal.status !== 404)
                return "stop";
            await call((saved, limit) =>
                saved.removeWorkspace(owner, tenant, limit),
            );
            return "go on";
        }
        const plants = sites.value.results.flatMap(({ id, plant }) =>
            plant
                ? [{ tenant, site: id, plant: plant.id, name: plant.name }]
                : [],
        );
        await call((saved, limit) =>
            saved.settle(
                owner,
                { workspaces, onScreen: { tenant, plants } },
                limit,
            ),
        );
        return "go on";
    };

    const readDays = async (plant: HeldPlant): Promise<Step> => {
        if (!eligible()) return "stop";
        const attemptedAt = Date.now();
        const marked = await call((saved, limit) =>
            saved.markAttempt(owner, plant, attemptedAt, limit),
        );
        if (!marked || !eligible()) return "stop";
        changed.delete(idOf(plant));
        let answer: DaysAnswer;
        try {
            answer = await send<DaysAnswer>({
                method: "GET",
                url: daysUrl(plant),
            });
        } catch (error) {
            const status = statusOf(error);
            if (status === 403) {
                await call((saved, limit) =>
                    saved.removeWorkspace(owner, plant.tenant, limit),
                );
                return "go on";
            }
            if (status === 404) {
                await call((saved, limit) =>
                    saved.removePlant(owner, plant, limit),
                );
                return "go on";
            }
            if (status === 429) {
                const named =
                    error instanceof ApiError ? (error.retryAfter ?? 0) : 0;
                const retryAt =
                    Date.now() + Math.max(named * 1_000, attemptGapMs);
                await call((saved, limit) =>
                    saved.setRetryAt(owner, retryAt, limit),
                );
            }
            return "stop";
        }
        await call((saved, limit) =>
            saved.writeDays(owner, plant, attemptedAt, answer, limit),
        );
        return "go on";
    };

    const held = () => call((saved, limit) => saved.held(owner, limit));

    const steps = async () => {
        if (!eligible() || !(await held())) return;
        if ((await settle()) === "stop") return;
        for (;;) {
            if (!eligible()) return;
            const copy = await held();
            const now = Date.now();
            if (!copy || (copy.retryAt !== null && now < copy.retryAt)) return;
            const next = due(copy.plants, now);
            if (!next || (await readDays(next)) === "stop") return;
        }
    };

    const ended = new AbortController();

    // Another window's run judges by its own screen, so a start that meets it waits instead of being dropped.
    const run = async () => {
        const locks = navigator.locks;
        if (!locks) return steps();
        await locks.request(runLock, { signal: ended.signal }, steps);
    };

    // A start while a run goes on runs once more after it: that run may have judged the plants before the start's cause.
    const start = () => {
        if (stopped) return;
        if (running) {
            again = true;
            return;
        }
        running = true;
        void run()
            .catch(() => {})
            .finally(() => {
                running = false;
                if (!again) return;
                again = false;
                start();
            });
    };

    const open = async () => {
        const plant = shownPlant();
        if (plant)
            await call((saved, limit) =>
                saved.openPlant(owner, plant, Date.now(), limit),
            );
        start();
    };

    const answered = (event: QueryCacheNotifyEvent) => {
        if (event.type !== "updated" || event.action.type !== "success") return;
        const { query } = event;
        if (!productionOf(query.queryKey, shownPlant())) return;
        const value = valueOf(query.state.data);
        const previous = seen.get(query.queryHash);
        seen.set(query.queryHash, value);
        if (previous === undefined || previous === value) return;
        const plant = shownPlant();
        if (plant) changed.add(idOf(plant));
        start();
    };

    const cache = context.client.getQueryCache();
    for (const query of cache.getAll())
        seen.set(query.queryHash, valueOf(query.state.data));
    const unsubscribeCache = cache.subscribe(answered);
    const unsubscribeOpened = subscribeOpened(() => void open());
    const onVisible = () => {
        if (visible()) start();
    };
    window.addEventListener("online", start);
    document.addEventListener("visibilitychange", onVisible);
    const timer = window.setInterval(onVisible, runEveryMs);
    void open();
    return () => {
        stopped = true;
        ended.abort();
        unsubscribeCache();
        unsubscribeOpened();
        window.removeEventListener("online", start);
        document.removeEventListener("visibilitychange", onVisible);
        window.clearInterval(timer);
    };
}
