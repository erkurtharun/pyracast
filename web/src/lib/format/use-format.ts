"use client";
import { useLocale } from "next-intl";
import { useMemo } from "react";
import {
    formatNumber,
    formatPercent,
    formatQuantity,
    formatRounded,
    formatSigned,
    type SummaryUnit,
} from "./number";
import {
    daysZoneLabel,
    deviceZone,
    formatClock,
    formatDate,
    formatDay,
    formatTime,
    formatZonedClock,
    formatZonedTime,
    intervalClock,
    zoneLabel,
    type Instant,
    type PlantDays,
} from "./time";

export function useFormat() {
    const locale = useLocale();
    return useMemo(
        () => ({
            number: (value: unknown, digits?: number) =>
                formatNumber(locale, value, digits),
            rounded: (value: unknown, unit: SummaryUnit) =>
                formatRounded(locale, value, unit),
            quantity: (value: unknown, unit: string, digits?: number) =>
                formatQuantity(locale, value, unit, digits),
            signed: (value: unknown, digits: number) =>
                formatSigned(locale, value, digits),
            percent: (fraction: unknown) => formatPercent(locale, fraction),
            day: (day: string) => formatDay(locale, day),
            date: (value: Instant, timeZone: string) =>
                formatDate(locale, value, timeZone),
            clock: (value: Instant, timeZone: string) =>
                formatClock(value, timeZone),
            time: (value: Instant, timeZone: string) =>
                formatTime(locale, value, timeZone),
            zonedTime: (value: Instant, timeZone: string) =>
                formatZonedTime(locale, value, timeZone),
            zonedClock: (value: Instant, timeZone: string) =>
                formatZonedClock(value, timeZone),
            // Workspace and account events are read in the device zone, so they name its offset.
            deviceTime: (value: Instant) =>
                formatZonedTime(locale, value, deviceZone()),
            zone: (timeZone: string, days?: PlantDays) =>
                days ? daysZoneLabel(timeZone, days) : zoneLabel(timeZone),
            intervals: (timeZone: string, timestamps: Iterable<string>) =>
                intervalClock(timeZone, timestamps),
        }),
        [locale],
    );
}
