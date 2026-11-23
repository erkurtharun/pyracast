import { createRequire } from "node:module";
import path from "node:path";
import type * as Client from "../../src/lib/api/client";
import type * as Errors from "../../src/lib/api/errors";
import type * as AuthApi from "../../src/lib/auth-api";
import type * as SessionClient from "../../src/lib/session-client";

const load = createRequire(__filename);
const source = path.resolve(__dirname, "../../src");

// The session store and the request lane hold the state of one document, which a session change
// replaces; a test that starts its own document loads them again.
export function openDocument() {
    for (const key of Object.keys(load.cache))
        if (key.startsWith(source)) delete load.cache[key];
    return {
        ...(load("../../src/lib/session-client") as typeof SessionClient),
        ...(load("../../src/lib/api/client") as typeof Client),
        ...(load("../../src/lib/api/errors") as typeof Errors),
        ...(load("../../src/lib/auth-api") as typeof AuthApi),
    };
}
