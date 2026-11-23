import path from "node:path";

export const coverage = process.env.PLAYWRIGHT_COVERAGE === "1";
export const productionServer = coverage || !!process.env.CI;
export const serverInspector = { host: "127.0.0.1", port: 9102 };
const coveragePort = 3102;
export const coverageOrigin = `http://127.0.0.1:${coveragePort}`;
// A public file outside the proxy matcher: readiness checks must not run authored server code.
export const coverageReadinessPath = "/identity/microsoft.svg";
export const serverSnapshots = path.resolve("coverage", "v8");

export function serverOrigin(coverageRun: boolean): string {
    return coverageRun ? coverageOrigin : "http://127.0.0.1:3100";
}

type WebServer = {
    command: string;
    env: Record<string, string>;
    url: string;
    reuseExistingServer: boolean;
    timeout: number;
};

export function webServer(coverageRun: boolean): WebServer {
    const origin = serverOrigin(coverageRun);
    const start = `npm start -- --port ${new URL(origin).port}`;
    const settings = { timeout: 120_000 };
    return coverageRun
        ? {
              ...settings,
              command: `${start} --inspect=${serverInspector.host}:${serverInspector.port}`,
              env: { NODE_V8_COVERAGE: serverSnapshots },
              url: `${origin}${coverageReadinessPath}`,
              reuseExistingServer: false,
          }
        : {
              ...settings,
              command: productionServer
                  ? start
                  : `npm run dev -- --port ${new URL(origin).port}`,
              env: {},
              url: origin,
              reuseExistingServer: !process.env.CI,
          };
}
