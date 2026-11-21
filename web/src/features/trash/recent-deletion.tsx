"use client";
import { useTranslations } from "next-intl";
import { createContext, useContext, useState, type ReactNode } from "react";
import { api } from "@/lib/api/client";
import { useRefresh } from "@/lib/api/query";
import { AsyncButton } from "@/ui/form";

type Plant = { tenant: number; region: number; name: string };
type Outcome =
    | { kind: "none" }
    | ({ kind: "deleted" } & Plant)
    | ({ kind: "restored" } & Plant);
type Recent = { outcome: Outcome; set: (outcome: Outcome) => void };

const RecentDeletion = createContext<Recent>({
    outcome: { kind: "none" },
    set: () => {},
});

export function RecentDeletionProvider({ children }: { children: ReactNode }) {
    const [outcome, set] = useState<Outcome>({ kind: "none" });
    return <RecentDeletion value={{ outcome, set }}>{children}</RecentDeletion>;
}

export function useRememberDeletion() {
    const { set } = useContext(RecentDeletion);
    return (plant: Plant) => set({ kind: "deleted", ...plant });
}

// A plant restored from the bin no longer offers its undo.
export function useForgetDeletion() {
    const { outcome, set } = useContext(RecentDeletion);
    return (region: number) => {
        if (outcome.kind !== "none" && outcome.region === region)
            set({ kind: "none" });
    };
}

// Stays until the person undoes or dismisses it, so a deletion can be taken back from the list.
export function DeletedNotice({ tenant }: { tenant: number }) {
    const t = useTranslations("RecycleBin");
    const refresh = useRefresh();
    const { outcome, set } = useContext(RecentDeletion);
    if (outcome.kind === "none" || outcome.tenant !== tenant) return null;
    const dismiss = (
        <button
            className="text-button"
            type="button"
            onClick={() => set({ kind: "none" })}
        >
            {t("dismiss")}
        </button>
    );
    if (outcome.kind === "restored")
        return (
            <div className="notice" role="status">
                <p>{t("restored", { name: outcome.name })}</p>
                {dismiss}
            </div>
        );
    return (
        <div className="notice" role="status">
            <p>{t("movedToBin", { name: outcome.name })}</p>
            <div className="button-group">
                <AsyncButton
                    variant="secondary"
                    onClick={async () => {
                        await api.post(
                            "/api/tenants/{tenant_id}/regions/{region_id}/restore/",
                            {
                                path: {
                                    tenant_id: tenant,
                                    region_id: outcome.region,
                                },
                            },
                        );
                        await refresh.workspace(tenant);
                        set({ ...outcome, kind: "restored" });
                    }}
                >
                    {t("undo")}
                </AsyncButton>
                {dismiss}
            </div>
        </div>
    );
}
