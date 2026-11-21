type Point = { latitude: number; longitude: number };

const coordinateNumber = /^-?\d+(?:\.\d+)?$/;

// The console's rule (admin-workspace.js): "lat, lon", "lat; lon" or two numbers apart by space.
export function coordinatePair(text: string): [string, string] | null {
    const input = text.trim();
    const separated = input.split(/[,;]/);
    const [latitude, longitude, ...rest] =
        separated.length === 1
            ? input.split(/\s+/)
            : separated.map((value) => value.trim());
    return latitude !== undefined &&
        longitude !== undefined &&
        !rest.length &&
        coordinateNumber.test(latitude) &&
        coordinateNumber.test(longitude)
        ? [latitude, longitude]
        : null;
}

export function zoneChoices(supported: readonly string[], current: string) {
    return [...new Set([...supported, current])].sort();
}

export function supportedZones(): readonly string[] {
    return Intl.supportedValuesOf("timeZone");
}

// The server stores coordinates rounded to 1e-6 degrees, so half of that is one stored value.
export function sameLocation(stored: Point, typed: Point) {
    return (
        Math.abs(stored.latitude - typed.latitude) <= 5e-7 &&
        Math.abs(stored.longitude - typed.longitude) <= 5e-7
    );
}
