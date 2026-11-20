import { MISSING } from "./number";

export type Instant = string | number | Date | null | undefined;

const MEDIUM: Intl.DateTimeFormatOptions = {
    dateStyle: "medium",
    timeStyle: "short",
};

function instant(value: Instant): Date | null {
    if (value == null) return null;
    const date = value instanceof Date ? value : new Date(value);
    return Number.isFinite(date.getTime()) ? date : null;
}

export function formatTime(
    locale: string,
    value: Instant,
    timeZone: string,
    options: Intl.DateTimeFormatOptions = MEDIUM,
): string {
    const date = instant(value);
    return date
        ? new Intl.DateTimeFormat(locale, { ...options, timeZone }).format(date)
        : MISSING;
}

export function deviceZone(): string {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

function zoneOffset(locale: string, timeZone: string, at: Date) {
    return new Intl.DateTimeFormat(locale, {
        timeZone,
        timeZoneName: "shortOffset",
    })
        .formatToParts(at)
        .find((part) => part.type === "timeZoneName")?.value;
}

export function zoneLabel(
    locale: string,
    timeZone: string,
    at: Date = new Date(),
): string {
    return `${timeZone} (${zoneOffset(locale, timeZone, at)})`;
}

export function plantDay(
    timeZone: string,
    offset = 0,
    now: Date = new Date(),
): string {
    const parts = new Intl.DateTimeFormat("en-US", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
    }).formatToParts(now);
    const part = (type: Intl.DateTimeFormatPartTypes) =>
        Number(parts.find((item) => item.type === type)?.value);
    return new Date(
        Date.UTC(part("year"), part("month") - 1, part("day") + offset),
    )
        .toISOString()
        .slice(0, 10);
}

export function intervalTimeFormatter(
    locale: string,
    timeZone: string,
    timestamps: Iterable<string>,
): Intl.DateTimeFormat {
    const offsets = new Set(
        Array.from(timestamps, (value) =>
            zoneOffset(locale, timeZone, new Date(value)),
        ),
    );
    return new Intl.DateTimeFormat(locale, {
        timeZone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        ...(offsets.size > 1 ? { timeZoneName: "shortOffset" as const } : {}),
    });
}
