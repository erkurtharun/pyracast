"use client";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { useState } from "react";
import type { PlantScope } from "@/lib/api";
import { sectionUnavailable } from "@/lib/on-demand";
import type { PlantAddress, PlantView } from "@/lib/routes";
import {
    ProductionHistory,
    type ShownDay,
} from "@/features/production/production-history";
import { ViewNav } from "@/ui/action";
import { Notice } from "@/ui/ui";

const ForecastPerformance = dynamic(() =>
    import("@/features/forecast/forecast-performance").then(
        (module) => module.ForecastPerformance,
        sectionUnavailable,
    ),
);

export function ProductionSection({
    scope,
    timezone,
    configurationId,
    view,
    day,
    baseDay,
    leaving,
    show,
    keep,
    onSaved,
}: {
    scope: PlantScope;
    timezone: string;
    configurationId: number | undefined;
    view: PlantView<"production">;
    day: string | undefined;
    baseDay: string | undefined;
    leaving: boolean;
    show: (next: PlantAddress) => void;
    keep: (next: PlantAddress) => void;
    onSaved: () => Promise<unknown>;
}) {
    const t = useTranslations("PlantWorkspace");
    const [history, setHistory] = useState<ShownDay | null>(null);
    const [shown, setShown] = useState({ configurationId, view, day });
    const [discarded, setDiscarded] = useState<string | null>(null);
    if (
        shown.configurationId !== configurationId ||
        shown.view !== view ||
        shown.day !== day
    ) {
        setShown({ configurationId, view, day });
        setDiscarded(
            shown.configurationId !== configurationId && history?.unsent
                ? history.date
                : null,
        );
    }
    return (
        <>
            <ViewNav
                label={t("productionViews")}
                views={[
                    ["actuals", t("actuals")],
                    ["comparison", t("comparison")],
                ]}
                current={view}
                onSelect={(next) => show({ view: next })}
            />
            {discarded !== null && discarded === history?.date && (
                <Notice>{t("configurationChanged")}</Notice>
            )}
            <fieldset
                disabled={leaving}
                inert={leaving}
                aria-busy={leaving || undefined}
            >
                {view === "comparison" ? (
                    <ForecastPerformance
                        key={configurationId}
                        scope={scope}
                        timezone={timezone}
                    />
                ) : (
                    <ProductionHistory
                        key={`history-${configurationId}`}
                        scope={scope}
                        configuration={configurationId}
                        timezone={timezone}
                        day={day}
                        baseDay={baseDay}
                        onDay={(next) => show({ date: next })}
                        onKeepDay={(next) => keep({ date: next })}
                        onShown={setHistory}
                        onSaved={onSaved}
                    />
                )}
            </fieldset>
        </>
    );
}
