export const plantSections = [
    "forecast",
    "production",
    "calibration",
    "outages",
    "settings",
] as const;
export type PlantSection = (typeof plantSections)[number];
export type ForecastView = "generation" | "irradiance";
export type ForecastAddress = { date?: string; view?: ForecastView };

const workspace = (tenant: number) => `/app/w/${tenant}/`;

export const routes = {
    entry: () => "/app/",
    signIn: (language: string) => `/app/?language=${language}`,
    signUp: (language: string) => `/app/?signup=1&language=${language}`,
    socialCallback: () => "/app/?social_callback=1",
    account: () => "/app/account/",
    invitation: () => "/app/invitation/",
    workspaces: () => "/app/w/",
    portfolio: workspace,
    members: (tenant: number) => `${workspace(tenant)}members/`,
    inbox: (tenant: number) => `${workspace(tenant)}inbox/`,
    trash: (tenant: number) => `${workspace(tenant)}trash/`,
    plant(
        tenant: number,
        site: number,
        section: PlantSection = "forecast",
        { date, view }: ForecastAddress = {},
    ) {
        const query = new URLSearchParams();
        if (date) query.set("date", date);
        if (view === "irradiance") query.set("view", view);
        const search = query.toString();
        return `${workspace(tenant)}plants/${site}/${section}/${search ? `?${search}` : ""}`;
    },
};

export type Screen =
    "portfolio" | "plant" | "members" | "inbox" | "trash" | "account" | "other";

export function screenOf(pathname: string): Screen {
    if (pathname === routes.account()) return "account";
    const match = pathname.match(/^\/app\/w\/\d+\/(?:([a-z]+)\/)?/);
    if (!match) return "other";
    const section = match[1];
    if (section === undefined) return "portfolio";
    if (section === "plants") return "plant";
    return section === "members" || section === "inbox" || section === "trash"
        ? section
        : "other";
}

export function routeId(value: string | undefined) {
    return value && /^[1-9]\d*$/.test(value) ? Number(value) : undefined;
}

export function isPlantSection(value: unknown): value is PlantSection {
    return plantSections.includes(value as PlantSection);
}

function isDay(value: string) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const day = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(day.getTime()) && day.toISOString().startsWith(value);
}

export function forecastAddress(params: URLSearchParams): ForecastAddress {
    const date = params.get("date");
    const view = params.get("view");
    return {
        ...(date && isDay(date) ? { date } : {}),
        ...(view === "generation" || view === "irradiance" ? { view } : {}),
    };
}
