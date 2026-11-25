"use client";
import { startTransition, useOptimistic } from "react";
import type { PlantScope } from "@/lib/api";
import { replaceAddress, useNavigate } from "@/lib/leave-guard";
import {
    plantView,
    routes,
    type PlantAddress,
    type PlantSection,
} from "@/lib/routes";

type Leaving = (section: "forecast" | "production" | "settings") => boolean;

export function usePlantNavigation(
    scope: PlantScope,
    tab: PlantSection,
    address: PlantAddress,
) {
    const go = useNavigate();
    const href = (section: PlantSection, next: PlantAddress) =>
        routes.plant(scope.tenant_id, scope.region_id, section, next);
    // Lives as long as the router transition: a committed, superseded, restored or failed
    // navigation returns it to the displayed address.
    const [pending, setPending] = useOptimistic({ section: tab, address });
    // Controls belong to the displayed section; only a pending change of that section is their base.
    const base = pending.section === tab ? pending.address : address;
    // Controls only the outgoing view has rest until the pending view change commits. A disabled
    // fieldset stops only form controls, so the outgoing body is also inert (disclosures, links).
    const leaving: Leaving = (section) =>
        pending.section === tab &&
        tab === section &&
        plantView(section, pending.address) !== plantView(section, address);
    const navigate = (
        section: PlantSection,
        next: PlantAddress,
        mode: "push" | "replace",
    ) =>
        startTransition(() => {
            if (go(href(section, next), mode))
                setPending({ section, address: next });
        });
    return {
        base,
        navigating:
            href(pending.section, pending.address) !== href(tab, address),
        leaving,
        selectTab: (next: PlantSection) =>
            navigate(next, next === tab ? base : {}, "push"),
        show: (next: PlantAddress) =>
            navigate(tab, { ...base, ...next }, "replace"),
        // Unmounts nothing, so it may change the address while anything is registered.
        keep: (next: PlantAddress) =>
            replaceAddress(href(tab, { ...address, ...next })),
    };
}
