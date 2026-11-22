import { readFileSync } from "node:fs";
import path from "node:path";
import { measure } from "./bundle-budget.ts";

const dist = path.resolve(process.env.NEXT_DIST_DIR ?? ".next");
const budget = JSON.parse(readFileSync("bundle-budget.json", "utf8"));
let over = false;
for (const { name, bytes, limit } of measure(dist, budget)) {
    over ||= bytes > limit;
    console.log(`${name}: ${bytes} bytes (limit ${limit})`);
}
if (over) {
    console.error(
        `A client bundle exceeds its recorded size by more than ${budget.tolerancePercent} %; record a larger size in bundle-budget.json only as a decision.`,
    );
    process.exitCode = 1;
}
