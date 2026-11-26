"use client";
import { useLocale } from "next-intl";
import { useMemo } from "react";
import {
    formatComputed,
    formatPercent,
    formatRecorded,
    formatRecordedQuantity,
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
    type IntervalBounds,
    type PlantDays,
} from "./time";

export function useFormat() {
    const locale = useLocale();
    return useMemo(
        () => ({
            recorded: (value: unknown) => formatRecorded(locale, value),
            recordedQuantity: (value: unknown, unit: string) =>
                formatRecordedQuantity(locale, value, unit),
            computed: (value: unknown) => formatComputed(locale, value),
            rounded: (value: unknown, unit: SummaryUnit) =>
                formatRounded(locale, value, unit),
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
            intervals: (
                timeZone: string,
                intervals: Iterable<IntervalBounds>,
            ) => intervalClock(timeZone, intervals),
        }),
        [locale],
    );
}
