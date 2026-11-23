import path from "node:path";

export const coverage = process.env.PLAYWRIGHT_COVERAGE === "1";
export const productionServer = coverage || !!process.env.CI;
export const serverInspector = { host: "127.0.0.1", port: 9102 };
export const coveragePort = 3102;
export const coverageOrigin = `http://127.0.0.1:${coveragePort}`;
// A public file outside the proxy matcher: readiness checks must not run authored server code.
export const coverageReadinessPath = "/identity/microsoft.svg";
export const serverSnapshots = path.resolve("coverage", "v8");
