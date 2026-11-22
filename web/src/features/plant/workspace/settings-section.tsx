"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { Plant, PlantScope, Region } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { plantExport } from "@/lib/exports";
import type { PlantAddress, PlantView } from "@/lib/routes";
import {
    PlantConfigurationFields,
    configurationInput,
    type Configuration,
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
import { ParameterConflict } from "./parameter-conflict";
import { conflictDraft, loadedDraft } from "./parameters-draft";

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
                            build={() =>
                                plantExport(
                                    {
                                        id: plant.id,
                                        name: plant.name,
                                        timezone,
                                    },
                                    region,
                                    plant.current_configuration ??
                                        plant.parameters,
                                )
                            }
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
                        onDeleted={() => void onDeleted()}
                    />
                </section>
            )}
        </>
    );
}

function isPlant(value: unknown): value is Plant {
    return (
        !!value &&
        typeof value === "object" &&
        "id" in value &&
        typeof value.id === "number" &&
        "parameters_revision" in value &&
        typeof value.parameters_revision === "number"
    );
}

function conflictPlant(error: unknown, id: number): Plant | null {
    if (
        !(error instanceof ApiError) ||
        error.code !== "plant_parameters_changed" ||
        !error.body ||
        typeof error.body !== "object" ||
        !("current" in error.body)
    )
        return null;
    const current = error.body.current;
    return isPlant(current) && current.id === id ? current : null;
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
    return (
        <section className="panel">
            <h2>{t("technicalDetails")}</h2>
            {plant.parameters ? (
                <TechnicalDetails
                    plant={plant}
                    parameters={plant.parameters}
                    scope={scope}
                    onSaved={onSaved}
                    onChanged={onChanged}
                />
            ) : (
                <Notice>{t("approvalDetailsRequired")}</Notice>
            )}
        </section>
    );
}

function TechnicalDetails({
    plant,
    parameters,
    scope,
    onSaved,
    onChanged,
}: {
    plant: Plant;
    parameters: Configuration;
    scope: PlantScope;
    onSaved: (plant: Plant) => void;
    onChanged: () => Promise<unknown>;
}) {
    const t = useTranslations("PlantWorkspace");
    const [draft, setDraft] = useState(() => loadedDraft(plant, parameters, 0));
    if (!draft.edited && draft.base !== plant.parameters_revision)
        setDraft(loadedDraft(plant, parameters, draft.key + 1));
    return (
        <ActionForm
            submit={t("saveConfiguration")}
            onEdit={() => {
                if (!draft.edited) setDraft({ ...draft, edited: true });
            }}
            onSubmit={async (data) => {
                const typed = configurationInput(data);
                try {
                    const saved = await api.put(
                        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/",
                        {
                            path: scope,
                            body: { ...typed, expected_revision: draft.base },
                        },
                    );
                    setDraft(
                        loadedDraft(
                            saved,
                            saved.parameters ?? typed,
                            draft.key + 1,
                        ),
                    );
                    onSaved(saved);
                } catch (error) {
                    const current = conflictPlant(error, plant.id);
                    if (current?.parameters) {
                        setDraft(
                            conflictDraft(
                                draft,
                                typed,
                                current,
                                current.parameters,
                            ),
                        );
                        onSaved(current);
                    }
                    throw error;
                }
                await onChanged();
            }}
        >
            {draft.conflict && <ParameterConflict conflict={draft.conflict} />}
            <PlantConfigurationFields
                key={draft.key}
                name={draft.values.name}
                configuration={draft.values}
            />
            {plant.current_configuration && (
                <p className="fine">{t("calibrationChangeNote")}</p>
            )}
        </ActionForm>
    );
}
