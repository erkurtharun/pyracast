type Searchable = {
    name: string;
    timezone: string;
    plant: { name: string } | null;
};

export function siteName(site: Searchable) {
    return site.plant?.name ?? site.name;
}

// Folds case by the locale and the Turkish dotted and dotless i together, so "İst" and "ist"
// both find "Istanbul".
function folded(text: string, locale: string) {
    return text
        .toLocaleLowerCase(locale)
        .normalize("NFD")
        .replace(/\p{M}/gu, "")
        .replaceAll("ı", "i");
}

export function matchesSearch(
    site: Searchable,
    query: string,
    locale: string,
): boolean {
    return folded(`${siteName(site)} ${site.timezone}`, locale).includes(
        folded(query, locale),
    );
}
