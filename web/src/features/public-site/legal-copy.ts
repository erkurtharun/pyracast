import en from "./legal-copy.en.json";
import tr from "./legal-copy.tr.json";

type Language = "en" | "tr";
type LegalKind = "privacy" | "terms";

type PublicOperator = {
    name: string;
    address: string;
    country: string;
    email: string;
};

type LegalSection = {
    id: string;
    title: string;
    paragraphs: string[];
    bullets?: string[];
    links?: { label: string; href: string }[];
};

export type LegalContent = {
    kind: LegalKind;
    title: string;
    intro: string;
    updated: string;
    sections: LegalSection[];
};

const operatorDetails = {
    name: "HARUN ERKURT",
    address: { en: "Istanbul", tr: "İstanbul" },
    country: "Türkiye",
    email: "info@pyracast.app",
};

const documents: Record<
    Language,
    Record<LegalKind, Omit<LegalContent, "kind">>
> = { en, tr };

function withOperator(text: string, operator: PublicOperator): string {
    return text.replaceAll(
        /\{(name|address|country|email)\}/g,
        (_, field: keyof PublicOperator) => operator[field],
    );
}

export function legalCopy(language: Language, kind: LegalKind): LegalContent {
    const operator: PublicOperator = {
        ...operatorDetails,
        address: operatorDetails.address[language],
    };
    const document = documents[language][kind];
    return {
        kind,
        ...document,
        sections: document.sections.map((section) => ({
            ...section,
            paragraphs: section.paragraphs.map((text) =>
                withOperator(text, operator),
            ),
        })),
    };
}
