import type { components } from "./schema";
import { browserErrors } from "@/i18n/browser";
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
    if (!response.ok) {
        const t = browserErrors();
        const message =
            errorMessage(data) || t("request", { status: response.status });
        const reference = requestReference(data);
        throw new ApiError(
            reference ? `${message} ${t("reference", { reference })}` : message,
            validationFields(data),
        );
    }
    return data as T;
}
function requestReference(data: unknown): string {
    return data &&
        typeof data === "object" &&
        "request_id" in data &&
        typeof data.request_id === "string"
        ? data.request_id
        : "";
}
function errorMessage(data: unknown): string {
    const t = browserErrors();
    if (typeof data === "string") {
        const messages: Record<string, string> = {
            location_already_exists: t("duplicateLocation"),
            location_already_has_plant: t("activePlant"),
        };
        if (messages[data]) return messages[data];
        const key = `codes.${data}` as Parameters<typeof t>[0];
        if (t.has(key)) return t(key);
        return /^[a-z][a-z0-9_]*$/.test(data) ? "" : data;
    }
    if (Array.isArray(data))
        return data.map(errorMessage).filter(Boolean).join(" ");
    if (data && typeof data === "object") {
        const detail = Object.entries(data)
            .filter(([key]) => !["code", "message", "request_id"].includes(key))
            .map(([key, value]) => {
                const message = errorMessage(value);
                return message
                    ? `${["detail", "non_field_errors"].includes(key) ? "" : `${key}: `}${message}`
                    : "";
            })
            .filter(Boolean)
            .join(" ");
        if (detail) return detail;
        if ("code" in data && typeof data.code === "string") {
            const key = `codes.${data.code}` as Parameters<typeof t>[0];
            return t.has(key) ? t(key) : "";
        }
    }
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
