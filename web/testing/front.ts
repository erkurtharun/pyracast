import { existsSync, readFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";

const WORKER_ADDRESS = "/app/sw.js";
const CONTROL_PREFIX = "/__front/";
const UNAVAILABLE = "No server answers behind the test front.";
const SCRIPT_FILES = new Map([
    ["withdrawal", "sw-withdraw.js"],
    ["previous", "sw-previous.js"],
]);
const UPSTREAM_STATES = new Set(["answering", "absent"]);
const UPSTREAM = new URL(required("FRONT_UPSTREAM"));

type State = { script: string; upstream: string };
const initial: State = { script: "build", upstream: "answering" };
let state = { ...initial };

function required(name: string) {
    const value = process.env[name];
    if (!value) throw new Error(`Set ${name} for the test front.`);
    return value;
}

function pathOf(request: http.IncomingMessage) {
    return new URL(request.url ?? "/", "http://front").pathname;
}

async function bodyOf(request: http.IncomingMessage) {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk as Buffer);
    return Buffer.concat(chunks).toString("utf8");
}

function scriptFile(script: string) {
    const file = SCRIPT_FILES.get(script);
    return file === undefined
        ? undefined
        : path.resolve(import.meta.dirname, file);
}

function servable(script: string) {
    const file = scriptFile(script);
    return script === "build" || (file !== undefined && existsSync(file));
}

function apply(setting: string, value: string) {
    if (setting === "script" && servable(value)) state.script = value;
    else if (setting === "upstream" && UPSTREAM_STATES.has(value))
        state.upstream = value;
    else return false;
    return true;
}

async function control(
    request: http.IncomingMessage,
    response: http.ServerResponse,
) {
    const setting = pathOf(request).slice(CONTROL_PREFIX.length);
    if (request.method === "DELETE" && setting === "state")
        state = { ...initial };
    else if (request.method === "PUT" && !apply(setting, await bodyOf(request)))
        return response.writeHead(400).end();
    response
        .writeHead(200, { "Content-Type": "application/json" })
        .end(JSON.stringify(state));
}

function withoutBodyHeaders(headers: http.IncomingHttpHeaders) {
    const kept = { ...headers };
    for (const name of [
        "content-encoding",
        "content-length",
        "etag",
        "last-modified",
        "transfer-encoding",
    ])
        delete kept[name];
    return kept;
}

function unavailable(response: http.ServerResponse) {
    if (response.headersSent) response.destroy();
    else
        response
            .writeHead(503, { "Content-Type": "text/plain; charset=utf-8" })
            .end(UNAVAILABLE);
}

function forward(request: http.IncomingMessage, response: http.ServerResponse) {
    if (state.upstream === "absent") {
        request.resume();
        return unavailable(response);
    }
    const file =
        pathOf(request) === WORKER_ADDRESS
            ? scriptFile(state.script)
            : undefined;
    const replacement = file === undefined ? undefined : readFileSync(file);
    const headers = { ...request.headers };
    if (replacement) {
        delete headers["if-none-match"];
        delete headers["if-modified-since"];
    }
    const upstream = http.request(
        {
            hostname: UPSTREAM.hostname,
            port: UPSTREAM.port,
            path: request.url,
            method: request.method,
            headers,
        },
        (answer) => {
            const status = answer.statusCode ?? 502;
            if (!replacement) {
                response.writeHead(status, answer.headers);
                answer.pipe(response);
                return;
            }
            answer.resume();
            response
                .writeHead(status, {
                    ...withoutBodyHeaders(answer.headers),
                    "content-length": String(replacement.length),
                })
                .end(replacement);
        },
    );
    upstream.on("error", () => unavailable(response));
    request.pipe(upstream);
}

http.createServer((request, response) => {
    if (pathOf(request).startsWith(CONTROL_PREFIX))
        void control(request, response);
    else forward(request, response);
}).listen(Number(required("FRONT_PORT")), "127.0.0.1");
