export const coverage = process.env.PLAYWRIGHT_COVERAGE === "1";
export const productionServer = coverage || !!process.env.CI;
