import { readFileSync } from "node:fs";
import path from "node:path";
import { budgetLimit, routeBytes } from "./bundle-budget.ts";

const dist = path.resolve(process.env.NEXT_DIST_DIR ?? ".next");
const budgets = JSON.parse(readFileSync("bundle-budget.json", "utf8"));
let over = false;
for (const [route, baseline] of Object.entries(budgets)) {
    const bytes = routeBytes(dist, route);
    const limit = budgetLimit(baseline);
    over ||= bytes > limit;
    console.log(
        `${route}: ${bytes} bytes (baseline ${baseline}, limit ${limit})`,
    );
}
if (over) {
    console.error("The client bundle exceeds its budget.");
    process.exitCode = 1;
}
