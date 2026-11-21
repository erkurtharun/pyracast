import type { components } from "./schema";
export type Tenant = components["schemas"]["TenantOutput"];
export type Region = components["schemas"]["RegionOutput"];
export type Plant = components["schemas"]["PlantOutput"];
export type Calibration = components["schemas"]["CalibrationOutput"];
type RegionalForecast = components["schemas"]["ForecastOutput"];
type ElectricalForecast = components["schemas"]["PlantForecastOutput"];
export type Forecast = Pick<RegionalForecast, "timezone" | "freshness"> & {
    result: RegionalForecast["result"] | ElectricalForecast["result"];
} & Partial<Omit<RegionalForecast, "result">> &
    Partial<Omit<ElectricalForecast, keyof RegionalForecast>>;
export type RegionScope = { tenant_id: number; region_id: number };
export type PlantScope = RegionScope & { plant_id: number };
