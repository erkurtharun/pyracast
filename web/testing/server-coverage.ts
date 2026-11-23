import { CDPClient, CoverageReport } from "monocart-coverage-reports";
import { serverInspector } from "../tests/support/server";
import { coverageOptions } from "./coverage-options";

// Playwright kills the web server without a graceful exit on Windows and by default on Linux, so
// NODE_V8_COVERAGE never writes at exit; the server writes its coverage on demand before that.
export default async function collectServerCoverage(): Promise<void> {
    const client = await CDPClient(serverInspector);
    if (!client)
        throw new Error(
            `No Next server inspector at ${serverInspector.host}:${serverInspector.port}`,
        );
    try {
        const directory = await client.writeCoverage();
        if (!directory)
            throw new Error("The Next server does not record V8 coverage");
        await new CoverageReport(coverageOptions).addFromDir(directory);
    } finally {
        await client.close();
    }
}
