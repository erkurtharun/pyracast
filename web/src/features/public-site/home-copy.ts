import en from "./home-copy.en.json";
import tr from "./home-copy.tr.json";

export type PublicLanguage = "en" | "tr";

type HomeCopy = {
    nav: string;
    startNav: string;
    login: string;
    signup: string;
    skip: string;
    privacy: string;
    terms: string;
    contact: string;
    footer: string;
    eyebrow: string;
    title: string;
    intro: string;
    secondary: string;
    chartTitle: string;
    chartAxis: string;
    chartDescription: string;
    forecast: string;
    actual: string;
    chartNote: string;
    productEyebrow: string;
    productTitle: string;
    features: readonly { title: string; text: string }[];
    startEyebrow: string;
    startTitle: string;
    startText: string;
    updated: string;
    contents: string;
    back: string;
};

export const homeCopy: Record<PublicLanguage, HomeCopy> = { en, tr };
