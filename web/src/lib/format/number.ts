export const MISSING = "—";

export function formatNumber(
    locale: string,
    value: unknown,
    maximumFractionDigits = 3,
): string {
    return typeof value === "number" && Number.isFinite(value)
        ? new Intl.NumberFormat(locale, { maximumFractionDigits }).format(value)
        : MISSING;
}

export function formatQuantity(
    locale: string,
    value: unknown,
    unit: string,
    maximumFractionDigits = 3,
): string {
    const number = formatNumber(locale, value, maximumFractionDigits);
    return number === MISSING ? MISSING : `${number} ${unit}`;
}
