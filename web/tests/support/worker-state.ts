import { readFileSync } from "node:fs";
import path from "node:path";

const read = (file: string) => readFileSync(path.resolve(__dirname, file));

export const workerWithdrawn = read("../../public/app/sw.js").equals(
    read("../../testing/sw-withdraw.js"),
);

const feature = read("../../src/lib/saved/feature.ts").toString();
const figuresOn = /SAVED_FIGURES = (true|false);/.exec(feature)?.[1];
if (figuresOn === undefined)
    throw new Error("feature.ts no longer declares SAVED_FIGURES as a literal");

export const savedFiguresOff = figuresOn === "false";

// "figuresWithdrawn": the constant is off and the worker without its reader is still served.
export const workerState = workerWithdrawn
    ? "workerWithdrawn"
    : savedFiguresOff
      ? "figuresWithdrawn"
      : "figures";
