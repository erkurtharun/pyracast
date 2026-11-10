"use client";
import { useState } from "react";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { Empty, Notice, useCollection } from "./ui";

type DeletedRegion = components["schemas"]["TrashedRegionOutput"];
type DeletedPlant = components["schemas"]["TrashedPlantOutput"];

export function MoveToRecycleBin({
    kind,
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
                Delete {kind}
            </button>
        );
    return (
        <section className="notice" aria-label={`Delete ${kind} confirmation`}>
            <h2>Move {name} to the recycle bin?</h2>
            <p>
                {kind === "region"
                    ? "This region and its plants will leave your active workspace."
                    : "This plant will leave your active workspace."}{" "}
                You can restore {kind === "region" ? "the region" : "the plant"}{" "}
                and its saved estimates, production readings and calibration
                history for 30 days. After that, it and its associated data will
                be permanently deleted automatically.
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
                    {working ? "Moving…" : `Move ${kind} to recycle bin`}
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
            setMessage(`${row.name} restored. Find it in Overview.`);
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
                    <span className="eyebrow">YOUR WORKSPACE</span>
                    <h1>Recycle bin</h1>
                    <p>
                        Deleted regions and plants stay here for 30 days before
                        automatic permanent deletion.
                    </p>
                </div>
            </div>
            <p>
                Restore a region before restoring any plant inside it. Plants
                deleted separately stay in the recycle bin when their region is
                restored.
            </p>
            {error && <Notice error>{error}</Notice>}
            {message && <Notice>{message}</Notice>}
            {(
                [
                    ["region", regions],
                    ["plant", plants],
                ] as const
            ).map(([kind, resource]) => (
                <section className="panel" key={kind}>
                    <h2>
                        {kind === "region"
                            ? "Deleted regions"
                            : "Deleted plants"}
                    </h2>
                    {resource.loading ? (
                        <p role="status">Loading deleted {kind}s…</p>
                    ) : resource.error ? (
                        <Notice error>{resource.error}</Notice>
                    ) : resource.data?.results.filter(
                          (row) => !restored.includes(`${kind}-${row.id}`),
                      ).length ? (
                        <div
                            className="table-scroll"
                            tabIndex={0}
                            role="region"
                            aria-label={`Deleted ${kind}s table`}
                        >
                            <table>
                                <caption className="sr-only">
                                    Deleted {kind}s
                                </caption>
                                <thead>
                                    <tr>
                                        <th>Name</th>
                                        {kind === "plant" && <th>Region</th>}
                                        <th>Deleted</th>
                                        <th>Permanent deletion</th>
                                        <th>Restore</th>
                                    </tr>
                                </thead>
                                <tbody>
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
                                                <tr key={row.id}>
                                                    <th>{row.name}</th>
                                                    {kind === "plant" && (
                                                        <td>
                                                            {
                                                                (
                                                                    row as DeletedPlant
                                                                ).region_name
                                                            }
                                                        </td>
                                                    )}
                                                    <td>
                                                        <time
                                                            dateTime={
                                                                row.deleted_at
                                                            }
                                                        >
                                                            {date(
                                                                row.deleted_at,
                                                            )}
                                                        </time>
                                                    </td>
                                                    <td>
                                                        <time
                                                            dateTime={
                                                                row.purge_after
                                                            }
                                                        >
                                                            {date(
                                                                row.purge_after,
                                                            )}
                                                        </time>
                                                    </td>
                                                    <td>
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
                                                                Restore its
                                                                region first.
                                                            </p>
                                                        ) : !row.can_restore ? (
                                                            <p className="fine">
                                                                The 30-day
                                                                restore period
                                                                has ended.
                                                            </p>
                                                        ) : null}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                </tbody>
                            </table>
                        </div>
                    ) : (
                        <Empty title={`No deleted ${kind}s`}>
                            Your active {kind}s are in Overview.
                        </Empty>
                    )}
                </section>
            ))}
        </>
    );
}
