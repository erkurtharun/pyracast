import type { Tenant } from "@/lib/api";

const rememberedKey = "pyracast.workspace";

function remembered() {
    try {
        return Number(window.localStorage.getItem(rememberedKey));
    } catch {
        return undefined;
    }
}

export function rememberWorkspace(tenant: number) {
    try {
        window.localStorage.setItem(rememberedKey, String(tenant));
    } catch {
        // Browser privacy settings can disable local storage.
    }
}

export function chooseWorkspace(rows: readonly Tenant[]) {
    return rows.find((row) => row.id === remembered()) ?? rows[0];
}
