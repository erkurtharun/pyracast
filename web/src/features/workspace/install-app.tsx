"use client";
import { useTranslations } from "next-intl";
import { promptInstall, useInstallOption } from "@/lib/install-prompt";

export function InstallApp() {
    const t = useTranslations("InstallApp");
    const option = useInstallOption();
    if (option.kind === "unavailable") return null;
    if (option.kind === "prompt")
        return (
            <button
                type="button"
                className="text-button install-app"
                onClick={() => void promptInstall(option.event).catch(() => {})}
            >
                {t("install")}
            </button>
        );
    const steps = {
        browserMenu: t("browserMenu"),
        homeScreen: t("homeScreen"),
        dock: t("dock"),
    };
    return (
        <details className="install-app">
            <summary>{t("install")}</summary>
            <p>{steps[option.steps]}</p>
        </details>
    );
}
