"use client";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import type { PlantScope } from "@/lib/api";
import { sectionUnavailable } from "@/lib/on-demand";
import type { PlantAddress, PlantView } from "@/lib/routes";
import { ProductionHistory } from "@/features/production/production-history";
import { ViewNav } from "@/ui/action";

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
    onSaved: () => Promise<unknown>;
}) {
    const t = useTranslations("PlantWorkspace");
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
                        onSaved={onSaved}
                    />
                )}
            </fieldset>
        </>
    );
}
