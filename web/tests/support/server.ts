import path from "node:path";

export const productionServer =
    process.env.PLAYWRIGHT_COVERAGE === "1" || !!process.env.CI;
export const serverInspector = { host: "127.0.0.1", port: 9102 };
const coveragePort = 3102;
export const coverageLockPort = 9103;
export const coverageRunVariable = "PLAYWRIGHT_COVERAGE_RUN";
export const coverageOrigin = `http://127.0.0.1:${coveragePort}`;
// A public file outside the proxy matcher: readiness checks must not run authored server code.
export const coverageReadinessPath = "/identity/microsoft.svg";

export function coverageRun(
    env: Record<string, string | undefined> = process.env,
): string | undefined {
    if (env.PLAYWRIGHT_COVERAGE !== "1") return undefined;
    const run = env[coverageRunVariable];
    if (!run)
        throw new Error(
            `Coverage mode needs the run identifier that testing/run-coverage.mjs sets in ${coverageRunVariable}; start it with npm run test:coverage`,
        );
    return run;
}

export function serverSnapshots(run: string): string {
    return path.resolve("coverage", "v8", run);
}

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

export function webServer(run: string | undefined): WebServer {
    const origin = serverOrigin(!!run);
    const start = `npm start -- --port ${new URL(origin).port}`;
    const settings = { timeout: 120_000 };
    return run
        ? {
              ...settings,
              command: `${start} --inspect=${serverInspector.host}:${serverInspector.port}`,
              env: { NODE_V8_COVERAGE: serverSnapshots(run) },
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
