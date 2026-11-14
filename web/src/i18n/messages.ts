import enCore from "../../messages/en/core.json";
import trCore from "../../messages/tr/core.json";
import enForecast from "../../messages/en/forecast.json";
import trForecast from "../../messages/tr/forecast.json";
import enWorkspace from "../../messages/en/workspace.json";
import trWorkspace from "../../messages/tr/workspace.json";

export const messages = {
    en: { ...enCore, ...enForecast, ...enWorkspace },
    tr: { ...trCore, ...trForecast, ...trWorkspace },
};
