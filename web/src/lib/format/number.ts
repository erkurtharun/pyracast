export const MISSING = "—";

const UNIT_SPACE = " ";
const MAX_FRACTION_DIGITS = 20;

export type SummaryUnit = "MWh" | "MW" | "kWh/m²" | "W/m²";

const SUMMARY_DIGITS: Record<SummaryUnit, number> = {
    MWh: 2,
    MW: 2,
    "kWh/m²": 2,
    "W/m²": 0,
};

const showsZero = ({ type, value }: Intl.NumberFormatPart) =>
    (type !== "integer" && type !== "fraction") || /^0+$/.test(value);

function numberText(
    locale: string,
    value: unknown,
    options: Intl.NumberFormatOptions,
): string {
    if (typeof value !== "number" || !Number.isFinite(value)) return MISSING;
    const parts = new Intl.NumberFormat(locale, options).formatToParts(value);
    const zero = parts.every(showsZero);
    return parts
        .filter(({ type }) => !zero || type !== "minusSign")
        .map(({ value: text }) => text)
        .join("");
}

function decimalsOf(value: number): number {
    const [digits = "", exponent = "0"] = value.toExponential().split("e");
    const fraction = digits.split(".")[1]?.length ?? 0;
    return Math.min(
        Math.max(fraction - Number(exponent), 0),
        MAX_FRACTION_DIGITS,
    );
}

export function formatRecorded(locale: string, value: unknown): string {
    const digits =
        typeof value === "number" && Number.isFinite(value)
            ? decimalsOf(value)
            : 0;
    return numberText(locale, value, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

export function formatRecordedQuantity(
    locale: string,
    value: unknown,
    unit: string,
): string {
    const number = formatRecorded(locale, value);
    return number === MISSING ? MISSING : `${number}${UNIT_SPACE}${unit}`;
}

export function formatComputed(locale: string, value: unknown): string {
    return numberText(locale, value, { maximumFractionDigits: 3 });
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
