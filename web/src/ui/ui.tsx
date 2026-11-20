"use client";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import type { Loaded } from "@/lib/api/query";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";

export type Tone = "neutral" | "success" | "warning" | "danger";

export function RefreshFailed({ resource }: { resource: Loaded<unknown> }) {
    const t = useTranslations("Common");
    const format = useFormat();
    if (!resource.error || resource.data === undefined) return null;
    return (
        <div className="notice error" role="alert">
            <p>
                {t("refreshFailed", {
                    time: format.time(resource.updatedAt, deviceZone(), {
                        timeStyle: "short",
                    }),
                })}{" "}
                {resource.error}
            </p>
            <button
                className="button secondary"
                type="button"
                onClick={resource.retry}
            >
                {t("retry")}
            </button>
        </div>
    );
}
export function Notice({
    children,
    error = false,
}: {
    children: ReactNode;
    error?: boolean;
}) {
    return (
        <div
            className={`notice ${error ? "error" : ""}`}
            role={error ? "alert" : "status"}
        >
            {children}
        </div>
    );
}
export function Stat({
    label,
    value,
    unit,
    note,
}: {
    label: string;
    value: unknown;
    unit?: string;
    note?: string;
}) {
    const format = useFormat();
    return (
        <div className="stat">
            <span>{label}</span>
            <strong>
                {typeof value === "string" ? value : format.number(value)}{" "}
                <small>{unit}</small>
            </strong>
            {note && <span>{note}</span>}
        </div>
    );
}
export function Tag({
    children,
    tone = "neutral",
}: {
    children: ReactNode;
    tone?: Tone;
}) {
    return <span className={`tag tag-${tone}`}>{children}</span>;
}
export function JsonDetails({
    data,
    label,
}: {
    data: unknown;
    label?: string;
}) {
    const t = useTranslations("Common");
    return (
        <details>
            <summary>{label ?? t("technicalDetails")}</summary>
            <pre>{JSON.stringify(data, null, 2)}</pre>
        </details>
    );
}
export function Empty({
    title,
    children,
    page = false,
}: {
    title: string;
    children: ReactNode;
    page?: boolean;
}) {
    const Heading = page ? "h1" : "h3";
    return (
        <div className="empty">
            <svg
                className="empty-icon"
                viewBox="0 0 24 24"
                aria-hidden="true"
                focusable="false"
            >
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" />
            </svg>
            <Heading>{title}</Heading>
            <p>{children}</p>
        </div>
    );
}
