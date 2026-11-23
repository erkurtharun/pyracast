export const coverage = process.env.PLAYWRIGHT_COVERAGE === "1";
export const productionServer = coverage || !!process.env.CI;
export const serverInspector = { host: "127.0.0.1", port: 9102 };
