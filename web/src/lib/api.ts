import type { components } from "./schema";
export type Session = components["schemas"]["SessionOutput"];
export type Tenant = components["schemas"]["TenantOutput"];
export type Region = components["schemas"]["RegionOutput"];
export type LocationChangeRequest =
    components["schemas"]["LocationChangeOutput"];
export type Plant = components["schemas"]["PlantOutput"];
export type Calibration = components["schemas"]["CalibrationOutput"];
export type CalibrationAutomation =
    components["schemas"]["CalibrationAutomationOutput"];
type RegionalForecast = components["schemas"]["ForecastOutput"];
type ElectricalForecast = components["schemas"]["PlantForecastOutput"];
export type Forecast = Pick<
    RegionalForecast,
    "timezone" | "freshness" | "result"
> &
    Partial<RegionalForecast & ElectricalForecast>;
export type Page<T> = {
    results: T[];
    next: string | null;
    previous: string | null;
    count: number;
};
export class ApiError extends Error {
    constructor(
        message: string,
        readonly fields: Record<string, string> = {},
    ) {
        super(message);
    }
}
let sessionQueue: Promise<unknown> = Promise.resolve();
let sessionFlight: Promise<Session> | undefined;
export function request<T>(
    url: string,
    method = "GET",
    body?: unknown,
    token?: string,
): Promise<T> {
    if (!url.startsWith("/api/session/"))
        return send<T>(url, method, body, token);
    if (method === "GET" && url === "/api/session/") {
        if (!sessionFlight) {
            sessionFlight = sessionQueue
                .then(() => send<Session>(url))
                .finally(() => {
                    sessionFlight = undefined;
                });
            sessionQueue = sessionFlight.catch(() => {});
        }
        return sessionFlight as Promise<T>;
    }
    const mutation = sessionQueue.then(() => send<T>(url, method, body, token));
    sessionQueue = mutation.catch(() => {});
    return mutation;
}
async function send<T>(
    url: string,
    method = "GET",
    body?: unknown,
    token?: string,
): Promise<T> {
    const response = await fetch(url, {
        method,
        credentials: "same-origin",
        cache: "no-store",
        headers: {
            ...(body === undefined
                ? {}
                : { "Content-Type": "application/json" }),
            ...(token ? { "X-CSRFToken": token } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data: unknown =
        response.status === 204
            ? null
            : await response.json().catch(() => null);
    if (!response.ok)
        throw new ApiError(
            errorMessage(data) ||
                `Request failed (${response.status}). Please try again.`,
            validationFields(data),
        );
    return data as T;
}
function errorMessage(data: unknown): string {
    if (typeof data === "string") {
        const messages: Record<string, string> = {
            location_already_exists:
                "This location is already in your company’s portfolio. Open the existing plant.",
            location_already_has_plant:
                "This location already has an active plant.",
        };
        return messages[data] ?? data;
    }
    if (Array.isArray(data)) return data.map(errorMessage).join(" ");
    if (data && typeof data === "object")
        return Object.entries(data)
            .filter(([key]) => key !== "code")
            .map(
                ([key, value]) =>
                    `${key === "detail" ? "" : `${key}: `}${errorMessage(value)}`,
            )
            .join(" ");
    return "";
}

function validationFields(data: unknown): Record<string, string> {
    if (!data || typeof data !== "object" || !("detail" in data)) return {};
    const detail = data.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail))
        return {};
    return Object.fromEntries(
        Object.entries(detail)
            .filter(([key]) => !["detail", "non_field_errors"].includes(key))
            .map(([key, value]) => [key, errorMessage(value)]),
    );
}
