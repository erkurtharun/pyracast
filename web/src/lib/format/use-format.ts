"use client";
import { useLocale } from "next-intl";
import { useMemo } from "react";
import { formatNumber, formatQuantity, formatSignificant } from "./number";
import {
    deviceZone,
    formatTime,
    intervalTimeFormatter,
    zoneLabel,
    type Instant,
} from "./time";

// Workspace events (inbox, invitations, recycle bin) are read in the device zone, so they name its offset.
const DEVICE_TIME: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "shortOffset",
};

export function useFormat() {
    const locale = useLocale();
    return useMemo(
        () => ({
            number: (value: unknown, digits?: number) =>
                formatNumber(locale, value, digits),
            significant: (value: unknown, digits: number) =>
                formatSignificant(locale, value, digits),
            quantity: (value: unknown, unit: string, digits?: number) =>
                formatQuantity(locale, value, unit, digits),
            time: (
                value: Instant,
                timeZone: string,
                options?: Intl.DateTimeFormatOptions,
            ) => formatTime(locale, value, timeZone, options),
            deviceTime: (value: Instant) =>
                formatTime(locale, value, deviceZone(), DEVICE_TIME),
            zone: (timeZone: string) => zoneLabel(locale, timeZone),
            intervals: (timeZone: string, timestamps: Iterable<string>) =>
                intervalTimeFormatter(locale, timeZone, timestamps),
        }),
        [locale],
    );
}
