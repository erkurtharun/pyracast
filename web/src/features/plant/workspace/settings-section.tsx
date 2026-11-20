"use client";
import { useTranslations } from "next-intl";
import type { Plant, PlantScope, Region } from "@/lib/api";
import { api } from "@/lib/api/client";
import type { PlantAddress, PlantView } from "@/lib/routes";
import {
    PlantConfigurationFields,
    configurationInput,
} from "@/features/portfolio/plant-configuration-fields";
import { MoveToRecycleBin } from "@/features/trash/recycle-bin";
import { ViewNav } from "@/ui/action";
import { CsvDownload } from "@/ui/csv-download";
import { ActionForm } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { CollectionSchedule } from "../collection-schedule";
import { RegionSettings } from "../region-settings";
import { Reminders } from "../reminders";

export function SettingsSection({
    plant,
    scope,
    region,
    timezone,
    view,
    leaving,
    show,
    onSaved,
    onChanged,
    onDeleted,
}: {
    plant: Plant;
    scope: PlantScope;
    region: Region;
    timezone: string;
    view: PlantView<"settings">;
    leaving: boolean;
    show: (next: PlantAddress) => void;
    onSaved: (plant: Plant) => void;
    onChanged: () => Promise<unknown>;
    onDeleted: () => Promise<void>;
}) {
    const t = useTranslations("PlantWorkspace");
    const canManage = useCanManageWorkspace();
    return (
        <>
            <ViewNav
                label={t("plantSettings")}
                views={[
                    ["configuration", t("technicalDetails")],
                    ["location", t("location")],
                    ["collection", t("dataCollection")],
                    ["reminders", t("emailReminders")],
                ]}
                current={view}
                onSelect={(next) => show({ view: next })}
            />
            <fieldset
                disabled={leaving}
                inert={leaving}
                aria-busy={leaving || undefined}
            >
                {view === "configuration" ? (
                    <PlantConfiguration
                        plant={plant}
                        scope={scope}
                        onSaved={onSaved}
                        onChanged={onChanged}
                    />
                ) : view === "location" ? (
                    <>
                        <RegionSettings region={region} scope={scope} />
                        <CsvDownload
                            label={t("downloadPlantCSV")}
                            filename={`plant-${plant.id}`}
                            records={[plant]}
                            metadata={{ timezone }}
                        />
                    </>
                ) : view === "collection" ? (
                    <CollectionSchedule scope={scope} region={region} />
                ) : (
                    <Reminders scope={scope} region={region} />
                )}
            </fieldset>
            {canManage && view === "configuration" && (
                <section className="panel">
                    <h2>{t("deletePlant")}</h2>
                    <p>{t("deletionHelp")}</p>
                    <MoveToRecycleBin
                        name={plant.name}
                        scope={scope}
                        onDeleted={onDeleted}
                    />
                </section>
            )}
        </>
    );
}

function PlantConfiguration({
    plant,
    scope,
    onSaved,
    onChanged,
}: {
    plant: Plant;
    scope: PlantScope;
    onSaved: (plant: Plant) => void;
    onChanged: () => Promise<unknown>;
}) {
    const t = useTranslations("PlantWorkspace");
    const configuration = plant.current_configuration;
    const metadata = configuration ?? plant.submission;
    return (
        <section className="panel">
            <h2>{t("technicalDetails")}</h2>
            {metadata ? (
                <ActionForm
                    submit={t("saveConfiguration")}
                    onSubmit={async (data) => {
                        onSaved(
                            await api.put(
                                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/",
                                { path: scope, body: configurationInput(data) },
                            ),
                        );
                        await onChanged();
                    }}
                >
                    <PlantConfigurationFields
                        name={plant.name}
                        configuration={metadata}
                    />
                    {configuration && (
                        <p className="fine">{t("calibrationChangeNote")}</p>
                    )}
                </ActionForm>
            ) : (
                <Notice>{t("approvalDetailsRequired")}</Notice>
            )}
        </section>
    );
}
