import { isLanguage, type Language } from "@/i18n/language";

const plantSections = [
    "forecast",
    "production",
    "calibration",
    "outages",
    "settings",
] as const;
export type PlantSection = (typeof plantSections)[number];
const plantViews = {
    forecast: ["generation", "irradiance"],
    production: ["actuals", "comparison"],
    settings: ["configuration", "location", "collection", "reminders"],
} as const;
type ViewSection = keyof typeof plantViews;
export type PlantView<S extends ViewSection> = (typeof plantViews)[S][number];
export type PlantAddress = { date?: string; view?: string };
const datedSections: readonly PlantSection[] = ["forecast", "production"];

function viewsOf(section: PlantSection): readonly string[] {
    return section in plantViews ? plantViews[section as ViewSection] : [];
}

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
    inbox: (tenant: number, request?: number) =>
        `${workspace(tenant)}inbox/${request ? `?request=${request}` : ""}`,
    trash: (tenant: number) => `${workspace(tenant)}trash/`,
    plant(
        tenant: number,
        site: number,
        section: PlantSection = "forecast",
        { date, view }: PlantAddress = {},
    ) {
        const query = new URLSearchParams();
        if (date && datedSections.includes(section)) query.set("date", date);
        const views = viewsOf(section);
        if (view && view !== views[0] && views.includes(view))
            query.set("view", view);
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

export function plantAddress(
    section: PlantSection,
    params: URLSearchParams,
): PlantAddress {
    const date = params.get("date");
    const view = params.get("view");
    return {
        ...(date && isDay(date) && datedSections.includes(section)
            ? { date }
            : {}),
        ...(view && viewsOf(section).includes(view) ? { view } : {}),
    };
}

export type SocialCallback = "none" | "succeeded" | "failed";
export const callbackParams = [
    "social_callback",
    "error",
    "error_process",
] as const;

export function appQuery(search: URLSearchParams): {
    signup: boolean;
    language?: Language;
    socialCallback: SocialCallback;
} {
    const language = search.get("language");
    return {
        signup: search.get("signup") === "1",
        ...(isLanguage(language) ? { language } : {}),
        socialCallback: !search.has("social_callback")
            ? "none"
            : search.has("error")
              ? "failed"
              : "succeeded",
    };
}

export function addressWithout(url: URL, names: readonly string[]) {
    const address = new URL(url);
    for (const name of names) address.searchParams.delete(name);
    return `${address.pathname}${address.search}${address.hash}`;
}

export function plantView<S extends ViewSection>(
    section: S,
    { view }: PlantAddress,
): PlantView<S> {
    const views: readonly string[] = plantViews[section];
    return (view && views.includes(view) ? view : views[0]) as PlantView<S>;
}
