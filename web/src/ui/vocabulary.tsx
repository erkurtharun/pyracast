"use client";
import { useTranslations } from "next-intl";
import { useCallback } from "react";
import type { Vocabulary } from "@/lib/vocabulary";
import { Tag, type Tone } from "./ui";

type Group = keyof Vocabulary;

const tones: { [G in Group]?: Partial<Record<Vocabulary[G], Tone>> } = {
    RegionStatus: { approved: "success", pending: "warning", rejected: "danger" },
    LocationChangeStatus: {
        approved: "success",
        pending: "warning",
        rejected: "danger",
    },
    Freshness: { fresh: "success", stale: "warning" },
    CalibrationStatus: {
        ready: "success",
        pending: "warning",
        failed: "danger",
        rejected: "danger",
    },
    ChangeRequestStatus: {
        approved: "success",
        pending: "warning",
        rejected: "danger",
    },
};

export function useLabel() {
    const t = useTranslations("Enums");
    return useCallback(
        <G extends Group>(group: G, value: Vocabulary[G]) =>
            t(`${group}.${value}`),
        [t],
    );
}

export function StatusTag<G extends Group>({
    group,
    value,
}: {
    group: G;
    value: Vocabulary[G];
}) {
    const label = useLabel();
    const tone = (tones[group] as Partial<Record<string, Tone>> | undefined)?.[
        value as string
    ];
    return <Tag tone={tone}>{label(group, value)}</Tag>;
}

// Codes the API returns as plain strings: a known code gets its message, any other the given fallback.
export function useCodeLabel(namespace: string) {
    const t = useTranslations(namespace);
    return useCallback(
        (key: string, fallback: string) => (t.has(key) ? t(key) : fallback),
        [t],
    );
}
