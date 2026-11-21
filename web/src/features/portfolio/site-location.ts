type Point = { latitude: number; longitude: number };

const coordinateNumber = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;

// A pair needs a separator that a decimal comma cannot be: a semicolon, white space, or a comma
// followed by white space; a bare comma only when a part has a decimal point. "41,0082" is
// one Turkish-style number, never 41 and 82.
function pairParts(input: string): string[] {
    if (input.includes(";")) return input.split(/\s*;\s*/);
    if (/,\s/.test(input)) return input.split(/\s*,\s+/);
    if (/\s/.test(input)) return input.split(/\s+/);
    const parts = input.split(",");
    return parts.some((part) => part.includes(".")) ? parts : [input];
}

export type CoordinateAxis = "latitude" | "longitude";

// HTML pattern syntax, which browsers compile whole with the v flag: an optional leading minus,
// no leading zero, at most one decimal comma or point, and the axis's range in degrees.
const coordinatePatterns: Record<CoordinateAxis, string> = {
    latitude: String.raw`-?(?:90(?:[.,]0+)?|[1-8]?\d(?:[.,]\d+)?)`,
    longitude: String.raw`-?(?:180(?:[.,]0+)?|(?:1[0-7]\d|[1-9]?\d)(?:[.,]\d+)?)`,
};

export function coordinatePattern(axis: CoordinateAxis) {
    return coordinatePatterns[axis];
}

// NaN when the field's pattern refuses the text, as Number() of any other non-number.
export function coordinateValue(axis: CoordinateAxis, text: string): number {
    return new RegExp(`^(?:${coordinatePatterns[axis]})$`, "u").test(text)
        ? Number(text.replace(",", "."))
        : Number.NaN;
}

export function coordinatePair(text: string): [string, string] | null {
    const [latitude, longitude, ...rest] = pairParts(text.trim());
    return latitude !== undefined &&
        longitude !== undefined &&
        !rest.length &&
        coordinateNumber.test(latitude) &&
        coordinateNumber.test(longitude)
        ? [latitude, longitude]
        : null;
}

// Browsers leave UTC out of their list; the server accepts every IANA zone.
export function zoneChoices(supported: readonly string[], current: string) {
    return [
        ...new Set([...supported, "UTC", ...(current ? [current] : [])]),
    ].sort();
}

type ZoneSource = { supportedValuesOf?: (key: "timeZone") => string[] };

export function supportedZones(
    source: ZoneSource = Intl,
): readonly string[] | null {
    return typeof source.supportedValuesOf === "function"
        ? source.supportedValuesOf("timeZone")
        : null;
}

// The server's canonical_coordinate: the shortest decimal text of the number, rounded half away
// from zero at six decimals.
export function canonicalCoordinate(value: number): number {
    const [mantissa = "0", exponent = "0"] = Math.abs(value)
        .toString()
        .split("e");
    const [whole = "0", fraction = ""] = mantissa.split(".");
    let digits = whole + fraction;
    let point = whole.length + Number(exponent);
    if (point < 0) {
        digits = "0".repeat(-point) + digits;
        point = 0;
    }
    digits = digits.padEnd(point + 7, "0");
    const micro =
        Number(digits.slice(0, point + 6)) +
        (Number(digits[point + 6]) >= 5 ? 1 : 0);
    return (Math.sign(value) * micro) / 1e6;
}

export function sameLocation(stored: Point, typed: Point) {
    return (
        canonicalCoordinate(stored.latitude) ===
            canonicalCoordinate(typed.latitude) &&
        canonicalCoordinate(stored.longitude) ===
            canonicalCoordinate(typed.longitude)
    );
}

type Binned = Point & { deleted_at: string; restore_blocker: string | null };

// The plant to offer for restore at a place, and how many other binned plants share it.
export function binnedAt<Row extends Binned>(
    rows: readonly Row[],
    place: Point,
): { row: Row; others: number } | null {
    const here = rows.filter((row) => sameLocation(row, place));
    const restorable = here
        .filter((row) => row.restore_blocker === null)
        .sort((a, b) => Date.parse(b.deleted_at) - Date.parse(a.deleted_at));
    const [row] = restorable;
    return row ? { row, others: here.length - 1 } : null;
}
