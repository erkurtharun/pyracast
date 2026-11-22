import core from "../../messages/tr/core.json";
import forecast from "../../messages/tr/forecast.json";
import shell from "../../messages/tr/shell.json";
import workspace from "../../messages/tr/workspace.json";

export const turkish = { ...shell, ...core, ...forecast, ...workspace };
