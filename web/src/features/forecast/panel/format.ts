"use client";
import { useLocale } from "next-intl";

export function useForecastNumber() {
    const locale = useLocale();
    return (value: unknown): string =>
        typeof value === "number"
            ? new Intl.NumberFormat(locale, {
                  maximumFractionDigits: 3,
              }).format(value)
            : "—";
}
