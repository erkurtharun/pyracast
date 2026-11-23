import { readFileSync } from "node:fs";
import path from "node:path";

const read = (file: string) => readFileSync(path.resolve(__dirname, file));

export const workerWithdrawn = read("../public/app/sw.js").equals(
    read("sw-withdraw.js"),
);
