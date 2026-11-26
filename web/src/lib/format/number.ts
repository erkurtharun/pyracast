export const MISSING = "—";

const UNIT_SPACE = " ";

export type SummaryUnit = "MWh" | "MW" | "kWh/m²" | "W/m²";

const SUMMARY_DIGITS: Record<SummaryUnit, number> = {
    MWh: 2,
    MW: 2,
    "kWh/m²": 2,
    "W/m²": 0,
};

function numberText(
    locale: string,
    value: unknown,
    options: Intl.NumberFormatOptions,
): string {
    return typeof value === "number" && Number.isFinite(value)
        ? new Intl.NumberFormat(locale, {
              signDisplay: "negative",
              ...options,
          }).format(value)
        : MISSING;
}

export function formatNumber(
    locale: string,
    value: unknown,
    maximumFractionDigits = 3,
): string {
    return numberText(locale, value, { maximumFractionDigits });
}

export function formatRounded(
    locale: string,
    value: unknown,
    unit: SummaryUnit,
): string {
    const digits = SUMMARY_DIGITS[unit];
    return numberText(locale, value, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

export function formatQuantity(
    locale: string,
    value: unknown,
    unit: string,
    maximumFractionDigits = 3,
): string {
    const number = formatNumber(locale, value, maximumFractionDigits);
    return number === MISSING ? MISSING : `${number}${UNIT_SPACE}${unit}`;
}

export function formatSigned(
    locale: string,
    value: unknown,
    fractionDigits: number,
): string {
    return numberText(locale, value, {
        minimumFractionDigits: fractionDigits,
        maximumFractionDigits: fractionDigits,
        signDisplay: "exceptZero",
    }).replace("-", "−");
}

export function formatPercent(locale: string, fraction: unknown): string {
    return numberText(locale, fraction, {
        style: "percent",
        maximumFractionDigits: 0,
    });
}

// Files keep the full stored precision in the plain form the production import reads.
export function fileNumber(value: number): string {
    return Number.isFinite(value) ? String(value) : "";
}
