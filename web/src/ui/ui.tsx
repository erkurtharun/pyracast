"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, type ReactNode } from "react";
import type { Loaded } from "@/lib/api/query";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";

export type Tone = "neutral" | "success" | "warning" | "danger";

export function RefreshFailed({ resource }: { resource: Loaded<unknown> }) {
    const t = useTranslations("Common");
    const format = useFormat();
    if (!resource.error || resource.data === undefined) return null;
    return (
        <ReadFailed resource={resource}>
            {t("refreshFailed", {
                time: format.clock(resource.updatedAt, deviceZone()),
            })}{" "}
        </ReadFailed>
    );
}
export function ReadFailed({
    resource,
    children,
}: {
    resource: Pick<Loaded<unknown>, "error" | "retry">;
    children?: ReactNode;
}) {
    const t = useTranslations("Common");
    return (
        <div className="notice error" role="alert">
            <p>
                {children}
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
// `focus` takes the focus when the notice appears, for an outcome whose control was disabled or
// removed while it worked.
export function Notice({
    children,
    error = false,
    focus = false,
}: {
    children: ReactNode;
    error?: boolean;
    focus?: boolean;
}) {
    const notice = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (focus) notice.current?.focus();
    }, [focus]);
    return (
        <div
            ref={notice}
            className={`notice ${error ? "error" : ""}`}
            role={error ? "alert" : "status"}
            tabIndex={focus ? -1 : undefined}
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
                {typeof value === "string" ? value : format.number(value)}
                {unit && (
                    <>
                        {" "}
                        <small>{unit}</small>
                    </>
                )}
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
export function Empty({
    title,
    children,
    page = false,
}: {
    title: string;
    children?: ReactNode;
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
            {children && <p>{children}</p>}
        </div>
    );
}
