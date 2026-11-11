"use client";
import { useState } from "react";
import { request, type LocationChangeRequest, type Region } from "@/lib/api";
import { ActionForm, Field, Notice, Tag, useCollection } from "./ui";

export function RegionSettings({
    region,
    base,
    token,
    onUpdated,
}: {
    region: Region;
    base: string;
    token: string;
    onUpdated: (region: Region) => void;
}) {
    const [version, setVersion] = useState(0);
    return (
        <section className="panel">
            <h2>Edit region</h2>
            <ActionForm
                submit="Save name"
                onSubmit={async (data) => {
                    onUpdated(
                        await request<Region>(
                            base,
                            "PATCH",
                            { name: data.get("name") },
                            token,
                        ),
                    );
                }}
            >
                <Field
                    label="Region name"
                    name="name"
                    defaultValue={region.name}
                />
            </ActionForm>
            <details>
                <summary>Request location change</summary>
                <LocationRequests
                    key={version}
                    region={region}
                    base={base}
                    token={token}
                    onSubmitted={() => setVersion((value) => value + 1)}
                />
            </details>
        </section>
    );
}

function LocationRequests({
    region,
    base,
    token,
    onSubmitted,
}: {
    region: Region;
    base: string;
    token: string;
    onSubmitted: () => void;
}) {
    const requests = useCollection<LocationChangeRequest>(
        `${base}location-change-requests/`,
    );
    const pending = requests.data?.results.find(
        (row) => row.status === "pending",
    );
    return (
        <>
            <p>
                Current location: {region.latitude}°, {region.longitude}° ·{" "}
                {region.timezone}
            </p>
            <p>
                A location change requires administrator approval and a new
                connection verification.
            </p>
            {requests.loading ? (
                <p role="status">Loading requests…</p>
            ) : requests.error ? (
                <Notice error>{requests.error}</Notice>
            ) : pending ? (
                <div>
                    <Tag>{pending.status}</Tag>{" "}
                    <span>
                        {pending.latitude}°, {pending.longitude}° ·{" "}
                        {pending.timezone}
                    </span>
                </div>
            ) : (
                <ActionForm
                    submit="Submit location request"
                    onSubmit={async (data) => {
                        try {
                            await request<LocationChangeRequest>(
                                `${base}location-change-requests/`,
                                "POST",
                                {
                                    latitude: Number(data.get("latitude")),
                                    longitude: Number(data.get("longitude")),
                                    timezone: data.get("timezone"),
                                    reason: data.get("reason"),
                                    expected_revision: region.revision,
                                },
                                token,
                            );
                            onSubmitted();
                        } catch (error) {
                            const message =
                                error instanceof Error
                                    ? error.message
                                    : "Unable to submit the request.";
                            if (message.includes("region_revision_changed"))
                                throw new Error(
                                    "The region has changed. Return to the region list and reopen it.",
                                    { cause: error },
                                );
                            if (
                                message.includes(
                                    "location_change_already_pending",
                                )
                            )
                                throw new Error(
                                    "A different location request is already pending. Reopen the editor to see it.",
                                    { cause: error },
                                );
                            if (message.includes("location_unchanged"))
                                throw new Error(
                                    "Enter a location different from the current location.",
                                    { cause: error },
                                );
                            throw error;
                        }
                    }}
                >
                    <div className="form-grid">
                        <Field
                            label="Latitude (°)"
                            name="latitude"
                            type="number"
                            min={-90}
                            max={90}
                            step="any"
                            defaultValue={region.latitude}
                        />
                        <Field
                            label="Longitude (°)"
                            name="longitude"
                            type="number"
                            min={-180}
                            max={180}
                            step="any"
                            defaultValue={region.longitude}
                        />
                        <Field
                            label="IANA timezone"
                            name="timezone"
                            defaultValue={region.timezone}
                        />
                        <Field
                            label="Reason (optional)"
                            name="reason"
                            required={false}
                        />
                    </div>
                </ActionForm>
            )}
        </>
    );
}
