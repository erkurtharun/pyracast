"use client";
import { useTranslations } from "next-intl";
import { Tag } from "@/ui/ui";
import { StatusTag } from "@/ui/vocabulary";
import { provisional, type Run } from "./model";

export function CalibrationMarks({
    run,
    inUse,
}: {
    run: Pick<Run, "status" | "assessment" | "result">;
    inUse: boolean;
}) {
    const t = useTranslations("Calibrations");
    return (
        <span className="tags">
            <StatusTag group="CalibrationStatus" value={run.status} />
            {inUse && <Tag tone="success">{t("in_use")}</Tag>}
            {run.assessment === "provisional" && <Tag>{t("preliminary")}</Tag>}
            {provisional(run) && (
                <Tag tone="warning">{t("provisional_production")}</Tag>
            )}
        </span>
    );
}
