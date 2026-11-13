"use client";
import { useState } from "react";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { Empty, Notice, useCollection } from "./ui";

type DeletedRegion = components["schemas"]["TrashedRegionOutput"];
type DeletedPlant = components["schemas"]["TrashedPlantOutput"];

export function MoveToRecycleBin({
    name,
    url,
    token,
    onDeleted,
}: {
    kind: "region" | "plant";
    name: string;
    url: string;
    token: string;
    onDeleted: () => void;
}) {
    const [confirming, setConfirming] = useState(false);
    const [working, setWorking] = useState(false);
    const [error, setError] = useState("");
    if (!confirming)
        return (
            <button
                className="button secondary"
                onClick={() => setConfirming(true)}
            >
                Delete plant
            </button>
        );
    return (
        <section className="notice" aria-label="Delete plant confirmation">
            <h2>Move {name} to the recycle bin?</h2>
            <p>
                Collection will stop. You can restore this plant and its saved
                data for 30 days. After that, they are permanently deleted.
            </p>
            {error && <Notice error>{error}</Notice>}
            <div className="button-group">
                <button
                    className="button secondary"
                    disabled={working}
                    onClick={() => {
                        setConfirming(false);
                        setError("");
                    }}
                >
                    Cancel
                </button>
                <button
                    className="button"
                    disabled={working}
                    onClick={async () => {
                        setWorking(true);
                        setError("");
                        try {
                            await request(url, "DELETE", undefined, token);
                            onDeleted();
                        } catch (failure) {
                            setError((failure as Error).message);
                            setWorking(false);
                        }
                    }}
                >
                    {working ? "Moving…" : "Move plant to recycle bin"}
                </button>
            </div>
        </section>
    );
}

export function RecycleBin({
    tenant,
    token,
}: {
    tenant: number;
    token: string;
}) {
    const [version, setVersion] = useState(0);
    const [working, setWorking] = useState("");
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [restored, setRestored] = useState<string[]>([]);
    const base = `/api/tenants/${tenant}/`;
    const regions = useCollection<DeletedRegion>(
        `${base}trash/regions/`,
        version,
    );
    const plants = useCollection<DeletedPlant>(`${base}trash/plants/`, version);
    const date = (value: string) =>
        new Date(value).toLocaleString("en", {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            timeZoneName: "short",
        });
    async function restore(
        kind: "region" | "plant",
        row: DeletedRegion | DeletedPlant,
    ) {
        const key = `${kind}-${row.id}`;
        setWorking(key);
        setError("");
        setMessage("");
        const url =
            kind === "region"
                ? `${base}regions/${row.id}/restore/`
                : `${base}regions/${(row as DeletedPlant).region_id}/plants/${row.id}/restore/`;
        try {
            await request(url, "POST", {}, token);
            setRestored((current) => [...current, key]);
            setVersion((current) => current + 1);
            setMessage(`${row.name} restored. Find it in Plants.`);
        } catch (failure) {
            setError((failure as Error).message);
        } finally {
            setWorking("");
        }
    }
    return (
        <>
            <div className="page-heading">
                <div>
                    <h1>Recycle bin</h1>
                    <p>Deleted plants and their data are kept for 30 days.</p>
                </div>
            </div>
            {error && <Notice error>{error}</Notice>}
            {message && <Notice>{message}</Notice>}
            {(
                [
                    ["region", regions],
                    ["plant", plants],
                ] as const
            )
                .filter(
                    ([kind, resource]) =>
                        kind === "region" ||
                        resource.loading ||
                        resource.error ||
                        resource.data?.results.some(
                            (row) => !restored.includes(`${kind}-${row.id}`),
                        ),
                )
                .map(([kind, resource]) => (
                    <section className="panel" key={kind}>
                        <h2>
                            {kind === "region"
                                ? "Deleted plants"
                                : "Earlier plant records"}
                        </h2>
                        {resource.loading ? (
                            <p role="status">Loading deleted plants…</p>
                        ) : resource.error ? (
                            <Notice error>{resource.error}</Notice>
                        ) : resource.data?.results.filter(
                              (row) => !restored.includes(`${kind}-${row.id}`),
                          ).length ? (
                            <ul
                                className="trash-list"
                                aria-label={
                                    kind === "region"
                                        ? "Deleted plants"
                                        : "Earlier plant records"
                                }
                            >
                                {resource.data.results
                                    .filter(
                                        (row) =>
                                            !restored.includes(
                                                `${kind}-${row.id}`,
                                            ),
                                    )
                                    .map((row) => {
                                        const parentDeleted =
                                            "parent_deleted" in row &&
                                            row.parent_deleted;
                                        return (
                                            <li
                                                className="trash-item"
                                                key={row.id}
                                            >
                                                <div>
                                                    <strong>{row.name}</strong>
                                                    {kind === "plant" && (
                                                        <p className="fine">
                                                            Location:{" "}
                                                            {
                                                                (
                                                                    row as DeletedPlant
                                                                ).region_name
                                                            }
                                                        </p>
                                                    )}
                                                    <dl className="trash-meta">
                                                        <div>
                                                            <dt>Deleted</dt>
                                                            <dd>
                                                                <time
                                                                    dateTime={
                                                                        row.deleted_at
                                                                    }
                                                                >
                                                                    {date(
                                                                        row.deleted_at,
                                                                    )}
                                                                </time>
                                                            </dd>
                                                        </div>
                                                        <div>
                                                            <dt>
                                                                Permanent
                                                                deletion
                                                            </dt>
                                                            <dd>
                                                                <time
                                                                    dateTime={
                                                                        row.purge_after
                                                                    }
                                                                >
                                                                    {date(
                                                                        row.purge_after,
                                                                    )}
                                                                </time>
                                                            </dd>
                                                        </div>
                                                    </dl>
                                                </div>
                                                <div>
                                                    <button
                                                        className="button secondary"
                                                        disabled={
                                                            !!working ||
                                                            !row.can_restore ||
                                                            !!parentDeleted
                                                        }
                                                        onClick={() =>
                                                            void restore(
                                                                kind,
                                                                row,
                                                            )
                                                        }
                                                    >
                                                        {working ===
                                                        `${kind}-${row.id}`
                                                            ? "Restoring…"
                                                            : `Restore ${row.name}`}
                                                    </button>
                                                    {parentDeleted ? (
                                                        <p className="fine">
                                                            Restore its location
                                                            first.
                                                        </p>
                                                    ) : !row.can_restore ? (
                                                        <p className="fine">
                                                            The 30-day restore
                                                            period has ended.
                                                        </p>
                                                    ) : null}
                                                </div>
                                            </li>
                                        );
                                    })}
                            </ul>
                        ) : (
                            <Empty title="No deleted plants">
                                Nothing to restore.
                            </Empty>
                        )}
                    </section>
                ))}
        </>
    );
}
