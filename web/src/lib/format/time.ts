import { MISSING } from "./number";

export type Instant = string | number | Date | null | undefined;
export type PlantDays = readonly [first: string, last: string];
export type IntervalBounds = readonly [start: string, end: string];

const DAY_MS = 86_400_000;
const LONG_MONTH_LANGUAGES = new Set(["tr"]);

function instant(value: Instant): Date | null {
    if (value == null) return null;
    const date = value instanceof Date ? value : new Date(value);
    return Number.isFinite(date.getTime()) ? date : null;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(
    key: string,
    build: () => Intl.DateTimeFormat,
): Intl.DateTimeFormat {
    let built = formatters.get(key);
    if (!built) {
        built = build();
        formatters.set(key, built);
    }
    return built;
}

const wallClockFormat = (timeZone: string) =>
    formatter(
        `wall ${timeZone}`,
        () =>
            new Intl.DateTimeFormat("en-US", {
                timeZone,
                hourCycle: "h23",
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
            }),
    );

// Built from parts: the en-GB short September is "Sept" in current ICU data.
const calendarFormat = (locale: string, timeZone: string) =>
    formatter(
        `calendar ${locale} ${timeZone}`,
        () =>
            new Intl.DateTimeFormat(locale, {
                timeZone,
                day: "numeric",
                month: LONG_MONTH_LANGUAGES.has(
                    new Intl.Locale(locale).language,
                )
                    ? "long"
                    : "short",
                year: "numeric",
            }),
    );

function wallClock(at: Date, timeZone: string) {
    const parts = wallClockFormat(timeZone).formatToParts(at);
    const part = (type: Intl.DateTimeFormatPartTypes) =>
        Number(parts.find((item) => item.type === type)?.value);
    return {
        year: part("year"),
        month: part("month"),
        day: part("day"),
        hour: part("hour"),
        minute: part("minute"),
        second: part("second"),
    };
}

const pad = (value: number, width = 2) => String(value).padStart(width, "0");

function calendarDate(locale: string, at: Date, timeZone: string): string {
    const parts = calendarFormat(locale, timeZone).formatToParts(at);
    const part = (type: Intl.DateTimeFormatPartTypes) =>
        parts.find((item) => item.type === type)?.value;
    return `${part("day")} ${part("month")} ${part("year")}`;
}

function clockOf(at: Date, timeZone: string): string {
    const { hour, minute } = wallClock(at, timeZone);
    return `${pad(hour)}:${pad(minute)}`;
}

function zoned(text: string, at: Date, timeZone: string): string {
    return `${text} (${zoneLabel(timeZone, at)})`;
}

export function formatDay(locale: string, day: string): string {
    const date = /^\d{4}-\d{2}-\d{2}$/.test(day)
        ? instant(`${day}T00:00:00Z`)
        : null;
    return date ? calendarDate(locale, date, "UTC") : MISSING;
}

export function formatDate(
    locale: string,
    value: Instant,
    timeZone: string,
): string {
    const date = instant(value);
    return date ? calendarDate(locale, date, timeZone) : MISSING;
}

export function formatClock(value: Instant, timeZone: string): string {
    const date = instant(value);
    return date ? clockOf(date, timeZone) : MISSING;
}

export function formatTime(
    locale: string,
    value: Instant,
    timeZone: string,
): string {
    const date = instant(value);
    return date
        ? `${calendarDate(locale, date, timeZone)}, ${clockOf(date, timeZone)}`
        : MISSING;
}

export function formatZonedTime(
    locale: string,
    value: Instant,
    timeZone: string,
): string {
    const date = instant(value);
    return date
        ? zoned(formatTime(locale, date, timeZone), date, timeZone)
        : MISSING;
}

export function formatZonedClock(value: Instant, timeZone: string): string {
    const date = instant(value);
    return date ? zoned(clockOf(date, timeZone), date, timeZone) : MISSING;
}

// ISO 8601 on the plant's wall clock with its UTC offset: the form exports write and the import reads.
export function plantTimestamp(
    value: Instant,
    timeZone: string,
): string | null {
    const date = instant(value);
    if (!date) return null;
    const { year, month, day, hour, minute, second } = wallClock(
        date,
        timeZone,
    );
    const wall = Date.UTC(year, month - 1, day, hour, minute, second);
    const offset = Math.round(
        (wall - Math.floor(date.getTime() / 1000) * 1000) / 60_000,
    );
    const size = Math.abs(offset);
    const sign = offset < 0 ? "-" : "+";
    return (
        `${pad(year, 4)}-${pad(month)}-${pad(day)}` +
        `T${pad(hour)}:${pad(minute)}:${pad(second)}` +
        `${sign}${pad(Math.floor(size / 60))}:${pad(size % 60)}`
    );
}

export function toPlantInput(value: Instant, timeZone: string): string {
    return plantTimestamp(value, timeZone)?.slice(0, 16) ?? "";
}

function offsetAt(at: number, timeZone: string): number {
    const stamp = plantTimestamp(at, timeZone);
    return stamp ? Date.parse(`${stamp.slice(0, 19)}Z`) - at : Number.NaN;
}

// The instant of a plant wall-clock input: null for a time the zone skips; a repeated hour gives its first occurrence.
export function fromPlantInput(value: string, timeZone: string): string | null {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null;
    const wall = Date.parse(`${value}:00Z`);
    if (!Number.isFinite(wall)) return null;
    const [first] = [wall - DAY_MS, wall, wall + DAY_MS]
        .map((probe) => wall - offsetAt(probe, timeZone))
        .filter((candidate) => toPlantInput(candidate, timeZone) === value)
        .sort((a, b) => a - b);
    return first === undefined ? null : new Date(first).toISOString();
}

export function deviceZone(): string {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

export function zoneLabel(timeZone: string, at: Date = new Date()): string {
    const offset = Math.round(offsetAt(at.getTime(), timeZone) / 60_000);
    if (!offset) return "UTC";
    const size = Math.abs(offset);
    const minutes = size % 60;
    return `UTC${offset < 0 ? "-" : "+"}${Math.floor(size / 60)}${minutes ? `:${pad(minutes)}` : ""}`;
}

export function daysZoneLabel(
    timeZone: string,
    [first, last]: PlantDays,
): string {
    const start = Date.parse(`${first}T00:00:00Z`);
    const end = Date.parse(`${last}T00:00:00Z`);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start)
        return zoneLabel(timeZone);
    const labels = new Set<string>();
    for (let day = start; day <= end; day += DAY_MS)
        for (const wall of [day, day + DAY_MS - 60_000])
            labels.add(
                zoneLabel(timeZone, new Date(wall - offsetAt(wall, timeZone))),
            );
    return [...labels].join(" / ");
}

export function plantDay(
    timeZone: string,
    offset = 0,
    now: Date = new Date(),
): string {
    const { year, month, day } = wallClock(now, timeZone);
    return new Date(Date.UTC(year, month - 1, day + offset))
        .toISOString()
        .slice(0, 10);
}

export function intervalClock(
    timeZone: string,
    intervals: Iterable<IntervalBounds>,
): (value: Instant) => string {
    const offsets = new Set(
        Array.from(intervals).flatMap((bounds) =>
            bounds.map((value) => zoneLabel(timeZone, new Date(value))),
        ),
    );
    return offsets.size > 1
        ? (value) => formatZonedClock(value, timeZone)
        : (value) => formatClock(value, timeZone);
}
