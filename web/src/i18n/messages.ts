import { english as en } from "./en";
import { turkish as tr } from "./tr";

// Both full catalogues, for server rendering only; the browser loads the active one through ./catalogue.
export const messages = { en, tr };
