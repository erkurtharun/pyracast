const DATABASE = "pyracast-saved";
const FORMAT = 1;
const READ_LIMIT_MS = 3000;
const DAY_MS = 86_400_000;
const MAX_TIME = 8.64e15;
const COPY_DAYS = 90;
const MAX_ROWS = 52;
const LANGUAGES = ["en", "tr"];

const PAGE_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
};

const DEVICE_TIME = {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "shortOffset",
};

const TEXTS = {
    en: {
        title: "Pyracast cannot be reached",
        unreachable: "The server did not answer. Try again in a moment.",
        tryAgain: "Try again",
        invitation:
            "If you came here from an invitation link, open that link again from the email.",
        showingSaved: "Pyracast cannot be reached · Saved on this device",
        dayNavigation: "Saved day navigation",
        views: "Saved plant figures",
        fullDay: "Full-day chart",
        intervalValues: "Interval values",
        localTime: "Plant local time",
        daylight: "Daylight",
        noDaylight: "No daylight on this plant day.",
        allZero: "All plotted interval values are zero.",
        invalidReadings:
            "Invalid readings are available in the interval table.",
        marks: "Short lines mark zero. Gaps mark unavailable values.",
        savedPlants: "Figures saved on this device",
        savedPlantsLink: "Saved plants",
        expired:
            "The saved figures were removed from this device because no one signed in here for 90 days or the device clock was changed.",
        removed: "The saved figures were removed from this device.",
        remove: "Remove saved figures from this device",
        confirmHeading: "Remove saved figures from this device?",
        confirmText:
            "Every saved plant and day is removed from this browser. While keeping figures stays on, they are saved again after the next sign-in.",
        confirm: "Remove saved figures",
        keep: "Keep the saved figures",
        read: (time, age) => `Read from the server on ${time}, ${age}.`,
        notRead: "Not read from the server yet.",
        age: (hours) =>
            hours < 1
                ? "less than an hour ago"
                : hours < 24
                  ? `${hours} ${hours === 1 ? "hour" : "hours"} ago`
                  : `${Math.floor(hours / 24)} ${hours < 48 ? "day" : "days"} ago`,
        kinds: { forecast: "Forecast", production: "Production" },
        previous: "Previous saved day",
        next: "Next saved day",
        source: (kind) => `Source: ${kind}.`,
        sourceKinds: {
            forecast: "Forecast",
            estimated_actuals: "Estimated actuals",
            mixed: "Estimated + forecast",
        },
        mixed: "Estimates for completed intervals where available; forecast elsewhere.",
        collected: (time) => `Source data collected on ${time}.`,
        staleReasons: {
            collection_overdue:
                "The newest provider collection is more than a day old.",
            connection_inactive:
                "The provider connection of this site is no longer active.",
        },
        reasons: {
            configuration_unavailable: "No configuration for this day.",
            forecast_unavailable: "No forecast for this day yet.",
            estimated_actuals_unavailable: "No estimated actuals for this day.",
            calculation_out_of_range: "This forecast could not be calculated.",
        },
        total: "Day total",
        affectedTotal: "With outages",
        partial: "partial estimate",
        impacts: { unknown_impact: "Outage impact is unknown." },
        curve: (plant, day, affected) =>
            `Energy per interval of ${plant} on ${day}${affected ? ", at normal capacity and with outages" : ""}. See the interval values for details.`,
        legend: "Left bar: without outages. Right bar: with outages.",
        intervals: "Energy per interval",
        interval: "Time interval",
        energy: "Energy (MWh)",
        affected: "With outages (MWh)",
        sources: "Sources",
        noRows: "No interval data is saved for this day.",
        complete: { true: "Complete day", false: "Incomplete day" },
        quality: "Quality",
        productionSource: "Source",
        mark: "Mark",
        provisional: "Provisional",
        qualities: {
            valid: "Valid",
            outage: "Outage",
            curtailed: "Curtailed",
            invalid: "Invalid",
            mixed: "Mixed",
        },
        noDays: "No figures of this kind are saved for this plant yet.",
        dayNotSaved: "This day is not saved on this device.",
        savedDays: "Saved days",
    },
    tr: {
        title: "Pyracast'e ulaşılamıyor",
        unreachable: "Sunucu yanıt vermedi. Biraz sonra tekrar deneyin.",
        tryAgain: "Tekrar dene",
        invitation:
            "Buraya bir davet bağlantısıyla geldiyseniz bağlantıyı e-postadan yeniden açın.",
        showingSaved: "Pyracast'e ulaşılamıyor · Bu cihazda kayıtlı",
        dayNavigation: "Kayıtlı günler arasında gezinme",
        views: "Kayıtlı santral verileri",
        fullDay: "Tam gün grafiği",
        intervalValues: "Aralık değerleri",
        localTime: "Santral saati",
        daylight: "Gün ışığı",
        noDaylight: "Bu santral gününde gün ışığı yok.",
        allZero: "Grafikteki tüm aralık değerleri sıfır.",
        invalidReadings: "Geçersiz ölçümler aralık tablosunda gösterilir.",
        marks: "Kısa çizgi sıfırı, boşluk kullanılabilir değer olmadığını gösterir.",
        savedPlants: "Bu cihazda kayıtlı rakamlar",
        savedPlantsLink: "Kayıtlı santraller",
        expired:
            "Bu cihazda 90 gündür oturum açılmadığı ya da cihaz saati değiştirildiği için kayıtlı rakamlar silindi.",
        removed: "Kayıtlı rakamlar bu cihazdan silindi.",
        remove: "Kayıtlı rakamları bu cihazdan sil",
        confirmHeading: "Kayıtlı rakamlar bu cihazdan silinsin mi?",
        confirmText:
            "Kayıtlı tüm santraller ve günler bu tarayıcıdan silinir. Rakamları saklama açık kaldıkça bir sonraki oturum açışta yeniden kaydedilir.",
        confirm: "Kayıtlı rakamları sil",
        keep: "Kayıtlı rakamları koru",
        read: (time, age) => `Sunucudan okunma: ${time}, ${age}.`,
        notRead: "Henüz sunucudan okunmadı.",
        age: (hours) =>
            hours < 1
                ? "bir saatten kısa süre önce"
                : hours < 24
                  ? `${hours} saat önce`
                  : `${Math.floor(hours / 24)} gün önce`,
        kinds: { forecast: "Tahmin", production: "Üretim" },
        previous: "Önceki kayıtlı gün",
        next: "Sonraki kayıtlı gün",
        source: (kind) => `Kaynak: ${kind}.`,
        sourceKinds: {
            forecast: "Tahmin",
            estimated_actuals: "Gerçekleşen tahmini",
            mixed: "Gerçekleşen tahmini + öngörü",
        },
        mixed: "Tamamlanan aralıklarda varsa gerçekleşen tahminleri, diğer aralıklarda öngörü.",
        collected: (time) => `Kaynak verinin alınma zamanı: ${time}.`,
        staleReasons: {
            collection_overdue:
                "Sağlayıcıdan alınan en yeni veri bir günden eski.",
            connection_inactive:
                "Bu sahanın sağlayıcı bağlantısı artık etkin değil.",
        },
        reasons: {
            configuration_unavailable: "Bu güne ait yapılandırma yok.",
            forecast_unavailable: "Bu güne ait tahmin henüz yok.",
            estimated_actuals_unavailable:
                "Bu güne ait gerçekleşen üretim tahmini yok.",
            calculation_out_of_range: "Bu tahmin hesaplanamadı.",
        },
        total: "Gün toplamı",
        affectedTotal: "Kesintiler dahil",
        partial: "kısmi tahmin",
        impacts: { unknown_impact: "Kesintinin etkisi bilinmiyor." },
        curve: (plant, day, affected) =>
            `${plant} santralinin ${day} günü aralık başına enerjisi${affected ? ", normal kapasitede ve kesintiler dahil" : ""}. Ayrıntılar aralık değerlerinde yer alır.`,
        legend: "Soldaki çubuk: kesinti olmadan. Sağdaki çubuk: kesintiler dahil.",
        intervals: "Aralık başına enerji",
        interval: "Zaman aralığı",
        energy: "Enerji (MWh)",
        affected: "Kesintiler dahil (MWh)",
        sources: "Kaynaklar",
        noRows: "Bu gün için kayıtlı aralık verisi yok.",
        complete: { true: "Tam gün", false: "Eksik gün" },
        quality: "Nitelik",
        productionSource: "Kaynak",
        mark: "İşaret",
        provisional: "Geçici",
        qualities: {
            valid: "Geçerli",
            outage: "Kesinti",
            curtailed: "Kısıtlanmış",
            invalid: "Geçersiz",
            mixed: "Karışık",
        },
        noDays: "Bu santral için bu türde kayıtlı rakam henüz yok.",
        dayNotSaved: "Bu gün bu cihazda kayıtlı değil.",
        savedDays: "Kayıtlı günler",
    },
};

const STYLE = `:root {
    color-scheme: light;
    --ink: #172f2a;
    --muted: #5c6d65;
    --success: #176b56;
    --line: #dfe6df;
    --surface: #fafbf8;
    --white: #fff;
    --status: #edf5ee;
    --blue: #3874a1;
    --daylight: #edf5ee;
    --axis: #788d80;
}
@media (prefers-color-scheme: dark) {
    :root {
        color-scheme: dark;
        --ink: #eef4ef;
        --muted: #b1c2b7;
        --success: #8bd4b9;
        --line: #475d51;
        --surface: #14221d;
        --white: #1b2d26;
        --status: #273c31;
        --blue: #9ac8eb;
        --daylight: #294032;
        --axis: #a4b7ad;
    }
}
body { margin: 0; font-family: "Segoe UI", system-ui, sans-serif; background: var(--surface); color: var(--ink); line-height: 1.5; }
main { max-width: 48rem; margin: 2.5rem auto; padding: 0 1rem; overflow-wrap: anywhere; }
section + section { margin-top: 2rem; padding-top: 2rem; border-top: 1px solid var(--line); }
a { color: var(--success); display: inline-block; min-height: 1.5rem; text-underline-offset: 0.2em; }
a:focus-visible, summary:focus-visible, .table:focus-visible { outline: 2px solid var(--success); outline-offset: 3px; }
li + li { margin-top: 0.25rem; }
.days { padding-left: 1.25rem; }
.saved-list { margin: 0; padding: 0; list-style: none; }
.saved-list li + li { margin-top: 0.5rem; }
.saved-list li { padding: 0.625rem 0.875rem; border: 1px solid var(--line); border-radius: 0.75rem; background: var(--white); }
.saved-list p { margin: 0; }
.saved-list a { display: flex; align-items: center; min-height: 2.5rem; font-weight: 600; }
.plant { max-width: 60rem; margin-block: clamp(1rem, 4vw, 3rem); }
.plant p, .plant h1, .plant h2, .plant h3 { margin: 0; }
.plant h1 { font-size: clamp(1.85rem, 5vw, 2.65rem); line-height: 1.12; letter-spacing: -0.025em; }
.plant h2 { font-size: clamp(1.15rem, 2.8vw, 1.4rem); line-height: 1.3; }
.plant h3 { margin-block: 0.75rem 0.25rem; }
.workspace, .day-meta, .meta, .saved-at, .chart-note { color: var(--muted); font-size: 0.875rem; }
.plant-header { margin-block-end: 1rem; }
.back, .retry, .day-navigation a, .view-links a, .view-links span, summary { display: inline-flex; align-items: center; min-height: 2.5rem; }
.back { margin-block-end: 0.5rem; }
.saved-state { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "status retry" "saved retry"; align-items: center; column-gap: 0.75rem; padding: 0.5rem 0.875rem; background: var(--status); border: 1px solid var(--line); border-radius: 0.75rem; }
.saved-label { grid-area: status; font-size: 0.875rem; }
.saved-at { grid-area: saved; }
.retry { grid-area: retry; font-size: 0.875rem; font-weight: 600; }
.view-links { display: flex; gap: 1.25rem; margin-block: 0.75rem 1px; border-bottom: 1px solid var(--line); }
.view-links a, .view-links span { padding-inline: 0.25rem; font-weight: 600; }
.view-links span { border-bottom: 2px solid var(--success); }
.figures { margin-block-start: 1rem; padding: clamp(0.875rem, 2.5vw, 1.5rem); border: 1px solid var(--line); border-radius: 1rem; background: var(--white); }
.day-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.375rem 1rem; flex-wrap: wrap; }
.day-navigation { display: flex; flex-wrap: wrap; gap: 0.25rem 0.75rem; }
.day-navigation a { font-size: 0.875rem; }
.day-content { display: grid; grid-template-columns: minmax(0, 1fr); gap: 0.75rem; margin-block-start: 1rem; }
.mark { font-weight: 600; }
.totals { display: flex; justify-content: flex-start; flex-wrap: wrap; gap: 0.5rem 1.25rem; }
.total { flex: 0 1 clamp(7rem, 32%, 9.75rem); min-width: min(100%, 7rem); }
.total > span, .total small { display: block; color: var(--muted); font-size: 0.875rem; }
.total strong { display: block; font-size: clamp(1.55rem, 4vw, 2.15rem); line-height: 1.2; font-variant-numeric: tabular-nums; }
.total strong span { font-size: 0.95rem; font-weight: 600; }
.meta { display: flex; gap: 0.25rem 1rem; flex-wrap: wrap; }
.chart-title { font-size: 0.9rem; }
.chart { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 0.25rem 0.5rem; margin-block-start: 0.375rem; }
.chart-y { display: flex; flex-direction: column; justify-content: space-between; text-align: end; }
.chart-y, .chart-time { color: var(--muted); font-size: 0.75rem; line-height: 1; font-variant-numeric: tabular-nums; }
.chart svg { display: block; width: 100%; height: clamp(9rem, 23vw, 13rem); outline: 1px solid var(--line); }
.chart-time { grid-column: 2; position: relative; height: 1rem; }
.chart-time span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.chart-time span:first-child { transform: none; }
.chart-time span:last-child { transform: translateX(-100%); }
.grid, .baseline { stroke: var(--axis); vector-effect: non-scaling-stroke; }
.grid { stroke-dasharray: 2 3; opacity: 0.5; }
.daylight { fill: var(--daylight); }
.energy { fill: var(--success); }
.affected { fill: var(--blue); }
.zero { stroke-width: 2px; vector-effect: non-scaling-stroke; }
.zero-energy { stroke: var(--success); }
.zero-affected { stroke: var(--blue); }
.chart-note { margin-block: 0.25rem; }
.chart-note p + p { margin-block-start: 0.125rem; }
.full-day[open] + .focused-chart { display: none; }
summary { display: list-item; min-height: auto; padding-block: 0.625rem; color: var(--success); cursor: pointer; font-weight: 600; }
.interval-values { border-top: 1px solid var(--line); }
.table { overflow-x: auto; margin-block-start: 0.25rem; }
table { border-collapse: collapse; width: 100%; overflow-wrap: break-word; }
caption { text-align: left; font-weight: 600; margin-bottom: 0.5rem; }
th, td { padding: 0.375rem 0.5rem; border-bottom: 1px solid var(--line); text-align: left; }
th:first-child, td:first-child { white-space: nowrap; }
td.number { text-align: right; font-variant-numeric: tabular-nums; }
.plant footer { text-align: end; margin-block-start: 1rem; font-size: 0.875rem; }
@media (max-width: 28rem) {
    .saved-state { grid-template-areas: "status retry" "saved saved"; }
    .day-navigation:not(:empty) { width: 100%; justify-content: space-between; }
    .chart-time span:nth-child(even):not(:last-child) { display: none; }
}`;

const NOTICE = noticePage({ languages: LANGUAGES });

function escape(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function noticePage({ languages, title, body, showingPlants = false }) {
    const sections = languages.map((language) => {
        const text = TEXTS[language];
        return `<section lang="${language}">
<h1>${escape(showingPlants ? text.savedPlantsLink : text.title)}</h1>
<p>${escape(showingPlants ? text.showingSaved : text.unreachable)}</p>
<p><a href="">${escape(text.tryAgain)}</a></p>
${showingPlants ? "" : `<p>${escape(text.invitation)}</p>`}
${body ? body(text, language) : ""}</section>`;
    });
    return `<!doctype html>
<html lang="${languages[0]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escape(title ?? languages.map((language) => TEXTS[language].title).join(" | "))}</title>
<style>
${STYLE}
</style>
</head>
<body>
<main>
${sections.join("\n")}
</main>
</body>
</html>
`;
}

self.addEventListener("install", () => {
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    if (self.registration.navigationPreload)
        event.waitUntil(self.registration.navigationPreload.enable());
});

self.addEventListener("fetch", (event) => {
    const { request } = event;
    if (
        request.mode === "navigate" &&
        request.method === "GET" &&
        request.url.startsWith(self.registration.scope)
    )
        event.respondWith(answer(event));
});

async function answer(event) {
    try {
        return (await event.preloadResponse) ?? (await fetch(event.request));
    } catch {
        return page(await saved(event.request.url).catch(() => NOTICE));
    }
}

function page(html) {
    return new Response(html, { status: 503, headers: PAGE_HEADERS });
}

async function saved(url) {
    const address = addressOf(new URL(url));
    const build = (copy, outcome) =>
        buildPage(copy, address, {
            now: copy.readAt,
            zone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            outcome,
        });
    const read = await limited((limit) =>
        connected(limit, (database) => readCopy(database, limit, address)),
    );
    if (!read.copy) return NOTICE;
    const removal = removalOf(read.copy, address);
    if (!removal) return build(read.copy);
    const written = await limited((limit) =>
        connected(limit, (database) =>
            readCopy(database, limit, address, removal),
        ),
    );
    if (!written.copy) return NOTICE;
    return build(
        written.copy,
        written.outcome === "removed" ? removal.sentence : undefined,
    );
}

function removalOf(copy, address) {
    const holder = copy.holder;
    if (!holder || copy.held === 0) return undefined;
    if (expired(holder, copy.readAt))
        return { holder, deactivates: false, sentence: "expired" };
    if (address.removal && address.token === holder.removal)
        return { holder, deactivates: true, sentence: "removed" };
    return undefined;
}

function addressOf(url) {
    const date = url.searchParams.get("date");
    const plant = url.pathname.match(
        /^\/app\/w\/([1-9]\d*)\/plants\/([1-9]\d*)(?=\/|$)(?:\/([^/]*))?/,
    );
    const tenant = Number(plant?.[1]);
    const site = Number(plant?.[2]);
    if (isId(tenant) && isId(site))
        return {
            tenant,
            site,
            kind: plant[3] === "production" ? "production" : "forecast",
            date: isDate(date) ? date : undefined,
        };
    return {
        entry: url.pathname === "/app/" && url.search === "",
        removal:
            url.pathname === "/app/" &&
            url.searchParams.get("saved") === "remove",
        token: url.searchParams.get("token") ?? undefined,
    };
}

function limited(work) {
    const deadline = performance.now() + READ_LIMIT_MS;
    const limit = { timedOut: false, transaction: undefined };
    limit.over = () => limit.timedOut || performance.now() >= deadline;
    const finished = work(limit).then(
        (copy) =>
            limit.over()
                ? { outcome: "timeout" }
                : { outcome: copy.outcome ?? "read", copy },
        () => ({ outcome: "failed" }),
    );
    let timer;
    const late = new Promise((resolve) => {
        timer = setTimeout(() => {
            limit.timedOut = true;
            resolve({ outcome: "timeout" });
            if (limit.transaction) abandoned(limit.transaction);
        }, READ_LIMIT_MS);
    });
    return Promise.race([finished, late]).finally(() => clearTimeout(timer));
}

// abort() throws once the transaction has begun to commit; that write then stands.
function abandoned(transaction) {
    try {
        transaction.abort();
        return true;
    } catch {
        return false;
    }
}

function opened() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DATABASE);
        request.onupgradeneeded = () => request.transaction.abort();
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
            const database = request.result;
            database.onversionchange = () => database.close();
            if (database.version === FORMAT) resolve(database);
            else {
                database.close();
                reject(new Error("Another format of the copy."));
            }
        };
    });
}

async function connected(limit, work) {
    const database = await opened();
    try {
        if (limit.over()) throw new Error("The copy opened after its limit.");
        return await work(database);
    } finally {
        database.close();
    }
}

function completed(transaction, result) {
    return new Promise((resolve, reject) => {
        transaction.oncomplete = () => resolve(result());
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
    });
}

function readCopy(database, limit, address, removal) {
    const transaction = database.transaction(
        ["holder", "plants", "days"],
        removal ? "readwrite" : "readonly",
    );
    limit.transaction = transaction;
    const copy = {
        holder: undefined,
        marks: [],
        held: 0,
        plants: [],
        plant: undefined,
        days: [],
        outcome: undefined,
        readAt: undefined,
    };
    const holders = transaction.objectStore("holder");
    const holder = holders.get("holder");
    holder.onsuccess = () => {
        copy.holder = holderOf(holder.result);
    };
    const marks = holders.getAllKeys();
    marks.onsuccess = () => {
        copy.marks = marks.result;
    };
    const plants = transaction.objectStore("plants");
    const keys = plants.getAllKeys();
    const values = plants.getAll();
    values.onsuccess = () => {
        copy.held = values.result.length;
        if (removal) {
            if (limit.over()) {
                abandoned(transaction);
                return;
            }
            copy.readAt = Date.now();
            copy.outcome = removed(transaction, copy, removal);
            if (copy.outcome === "removed") return;
        }
        copy.plants = values.result
            .map((value, index) => plantOf(value, keys.result[index]))
            .filter(Boolean);
        copy.plant =
            address.tenant === undefined
                ? undefined
                : copy.plants.find(
                      (held) =>
                          held.tenant === address.tenant &&
                          held.site === address.site,
                  );
        if (copy.plant) readDays(transaction, copy, address.kind);
    };
    return completed(transaction, () => {
        if (!removal) copy.readAt = Date.now();
        return copy;
    });
}

function readDays(transaction, copy, kind) {
    const { tenant, site, plant } = copy.plant;
    const prefix = [tenant, site, plant, kind];
    const range = IDBKeyRange.bound(prefix, [...prefix, []]);
    const days = transaction.objectStore("days");
    const keys = days.getAllKeys(range);
    const values = days.getAll(range);
    values.onsuccess = () => {
        copy.days = values.result
            .map((value, index) =>
                dayOf(value, keys.result[index], copy.plant, kind),
            )
            .filter(Boolean);
    };
}

const HOLDER_IDENTITY = ["user", "session", "active", "confirmedAt", "removal"];

function removed(transaction, copy, removal) {
    if (
        !copy.holder ||
        HOLDER_IDENTITY.some(
            (field) => copy.holder[field] !== removal.holder[field],
        )
    )
        return "changed";
    if (copy.held === 0) return "nothing";
    if (removal.sentence === "expired" && !expired(copy.holder, copy.readAt))
        return "changed";
    transaction.objectStore("plants").clear();
    transaction.objectStore("days").clear();
    if (removal.deactivates)
        transaction
            .objectStore("holder")
            .put({ ...copy.holder, active: false }, "holder");
    return "removed";
}

// The application's rule (store.ts `refuses`): nothing is shown for a person who turned keeping off here or for a session that is ending or ended.
function refused(holder, marks) {
    return marks.some(
        (mark) =>
            Array.isArray(mark) &&
            ((mark[0] === "off" && mark[1] === holder.user) ||
                (mark[0] === "ended" && mark[2] === holder.session)),
    );
}

function expired(holder, now) {
    return (
        now - holder.confirmedAt > COPY_DAYS * DAY_MS ||
        holder.confirmedAt - now > DAY_MS
    );
}

const isId = (value) => Number.isSafeInteger(value) && value > 0;
const isText = (value) => typeof value === "string";
const isFlag = (value) => typeof value === "boolean";
const isTime = (value) =>
    Number.isSafeInteger(value) && Math.abs(value) <= MAX_TIME;
const isFigure = (value) =>
    value === null || (typeof value === "number" && Number.isFinite(value));
const isEnergy = (value) => isFigure(value) && (value === null || value >= 0);
const orNull = (check) => (value) => value === null || check(value);
const isRecord = (value) =>
    typeof value === "object" && value !== null && !Array.isArray(value);
const isList = (value) =>
    Array.isArray(value) &&
    value.findIndex((_, index) => !Object.hasOwn(value, index)) === -1;
const sameKey = (key, expected) =>
    Array.isArray(key) &&
    key.length === expected.length &&
    key.every((part, index) => part === expected[index]);

function isDate(value) {
    if (!isText(value) || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const day = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(day.getTime()) && day.toISOString().startsWith(value);
}

const INSTANT =
    /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-](\d{2}):(\d{2}))$/;

function isInstant(value) {
    const parts = isText(value) && INSTANT.exec(value);
    if (!parts) return false;
    const [, date, hour, minute, second, offsetHour = 0, offsetMinute = 0] =
        parts;
    return (
        isDate(date) &&
        Number(hour) < 24 &&
        Number(minute) < 60 &&
        Number(second) < 60 &&
        Number(offsetHour) < 24 &&
        Number(offsetMinute) < 60 &&
        !Number.isNaN(Date.parse(value))
    );
}

const isBoundary = (value) => isInstant(value) && !value.includes(".");

function localDate(timeZone) {
    const format = new Intl.DateTimeFormat("en-US", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
    });
    return (instant) => {
        const parts = format.formatToParts(instant);
        const part = (type) => parts.find((item) => item.type === type).value;
        return `${part("year").padStart(4, "0")}-${part("month")}-${part("day")}`;
    };
}

function onDay(day) {
    const dateOf = localDate(day.timezone);
    return day.rows.every(
        (row) =>
            dateOf(Date.parse(row.start)) === day.date &&
            dateOf(Date.parse(row.end) - 1) === day.date,
    );
}

function ordered(rows) {
    let previous = -Infinity;
    return rows.every((row) => {
        const start = Date.parse(row.start);
        const end = Date.parse(row.end);
        const follows = previous <= start && start < end;
        previous = end;
        return follows;
    });
}

function isZone(value) {
    if (!isText(value)) return false;
    try {
        new Intl.DateTimeFormat("en", { timeZone: value });
        return true;
    } catch {
        return false;
    }
}

function matches(value, shape) {
    return (
        isRecord(value) &&
        Object.entries(shape).every(([field, check]) => check(value[field]))
    );
}

function holderOf(value) {
    return matches(value, {
        user: isId,
        language: (language) => LANGUAGES.includes(language),
        session: isText,
        active: isFlag,
        confirmedAt: isTime,
        removal: (token) => isText(token) && /^[0-9a-f]{32}$/i.test(token),
        retryAt: orNull(isTime),
    })
        ? value
        : undefined;
}

function plantOf(value, key) {
    return matches(value, {
        tenant: isId,
        site: isId,
        plant: isId,
        name: isText,
        workspace: isText,
        openedAt: orNull(isTime),
        attemptedAt: orNull(isTime),
        refreshedAt: orNull(isTime),
    }) && sameKey(key, [value.tenant, value.site, value.plant])
        ? value
        : undefined;
}

const rowsOf = (check, figured) => (rows) =>
    isList(rows) &&
    rows.length <= MAX_ROWS &&
    rows.every(check) &&
    (rows.length === 0 || rows.some(figured)) &&
    ordered(rows);

const FORECAST_DAY = {
    source_kind: isText,
    freshness: isText,
    stale_reason: orNull(isText),
    unavailable_reason: orNull(isText),
    collected_at: orNull(isInstant),
    total_mwh: isFigure,
    partial: isFlag,
    affected: orNull((affected) =>
        matches(affected, {
            status: isText,
            total_mwh: isFigure,
            partial: isFlag,
        }),
    ),
    rows: rowsOf(
        (row) =>
            matches(row, {
                start: isBoundary,
                end: isBoundary,
                mwh: isEnergy,
                affected_mwh: isEnergy,
                sources: orNull(
                    (sources) => isList(sources) && sources.every(isText),
                ),
            }),
        (row) => row.mwh !== null || row.affected_mwh !== null,
    ),
};

const PRODUCTION_DAY = {
    total_mwh: isFigure,
    complete: isFlag,
    rows: rowsOf(
        (row) =>
            matches(row, {
                start: isBoundary,
                end: isBoundary,
                mwh: isEnergy,
                quality: orNull(isText),
                source: orNull(isText),
                provisional: orNull(isFlag),
            }) &&
            [row.quality, row.source, row.provisional].every(
                (field) => (field === null) === (row.mwh === null),
            ),
        (row) => row.mwh !== null,
    ),
};

function forecastFits(day) {
    if (day.unavailable_reason === null)
        return (
            (day.source_kind === "mixed" ||
                day.rows.every((row) => row.sources === null)) &&
            (day.affected !== null ||
                day.rows.every((row) => row.affected_mwh === null))
        );
    return (
        day.freshness === "missing" &&
        day.stale_reason === null &&
        day.collected_at === null &&
        day.total_mwh === null &&
        day.partial === false &&
        day.affected === null &&
        day.rows.length === 0
    );
}

// Round the exclusive end outwards: a microsecond of daylight still overlaps the next interval.
function daylightEnd(stamp) {
    const submillisecond = /\.\d{3}(\d+)(?:Z|[+-])/.exec(stamp)?.[1];
    return Date.parse(stamp) + Number(/[1-9]/.test(submillisecond ?? ""));
}

function savedDaylightWindows(day) {
    const windows = day.daylight_windows;
    if (!Array.isArray(windows) || windows.length > MAX_ROWS) return undefined;
    const rows = [];
    for (const window of windows) {
        if (!matches(window, { starts_at: isInstant, ends_at: isInstant }))
            return undefined;
        rows.push({
            start: window.starts_at,
            end: new Date(daylightEnd(window.ends_at)).toISOString(),
        });
    }
    return ordered(rows) && onDay({ ...day, rows }) ? windows : undefined;
}

function dayOf(value, key, plant, kind) {
    if (
        !matches(value, {
            tenant: (tenant) => tenant === plant.tenant,
            site: (site) => site === plant.site,
            plant: (id) => id === plant.plant,
            kind: (stored) => stored === kind,
            date: isDate,
            timezone: isZone,
            ...(kind === "forecast" ? FORECAST_DAY : PRODUCTION_DAY),
        }) ||
        !sameKey(key, [
            plant.tenant,
            plant.site,
            plant.plant,
            kind,
            value.date,
        ]) ||
        (kind === "forecast" && !forecastFits(value)) ||
        !onDay(value)
    )
        return undefined;
    if (value.daylight_windows == null) return value;
    return { ...value, daylight_windows: savedDaylightWindows(value) };
}

function buildPage(copy, address, { now, zone, outcome }) {
    const holder = copy.holder;
    if (!holder) return NOTICE;
    const languages = [holder.language];
    const sentence = (key) => (text) => `<p>${escape(text[key])}</p>\n`;
    if (outcome) return noticePage({ languages, body: sentence(outcome) });
    if (!holder.active || expired(holder, now) || refused(holder, copy.marks))
        return noticePage({ languages });
    const plants = [...copy.plants].sort(
        (first, second) =>
            first.workspace.localeCompare(second.workspace) ||
            first.name.localeCompare(second.name),
    );
    const format = formats(holder.language, zone, now);
    if (address.removal && address.token === undefined && plants.length > 0)
        return noticePage({
            languages,
            title: TEXTS[holder.language].confirmHeading,
            body: (text) => confirmation(text, holder.removal),
        });
    if (copy.plant)
        return plantPage(
            holder.language,
            copy.plant,
            address,
            copy.days,
            format,
        );
    const showingPlants = address.entry === true && plants.length > 0;
    return noticePage({
        languages,
        showingPlants,
        title: showingPlants
            ? TEXTS[holder.language].savedPlantsLink
            : undefined,
        body: (text) => savedList(text, plants, format, !showingPlants),
    });
}

function formats(language, zone, now) {
    const number = new Intl.NumberFormat(language, {
        maximumFractionDigits: 3,
    });
    const device = new Intl.DateTimeFormat(language, {
        ...DEVICE_TIME,
        timeZone: zone,
    });
    const calendar = new Intl.DateTimeFormat(language, {
        dateStyle: "long",
        timeZone: "UTC",
    });
    return {
        now,
        number: (value) => (value === null ? "—" : number.format(value)),
        device: (value) => device.format(new Date(value)),
        day: (date) => calendar.format(new Date(`${date}T00:00:00Z`)),
    };
}

function plantLink(plant, kind, date) {
    return `/app/w/${plant.tenant}/plants/${plant.site}/${kind}/${date ? `?date=${date}` : ""}`;
}

function link(href, label) {
    return `<a href="${escape(href)}">${escape(label)}</a>`;
}

function readTime(text, plant, format) {
    if (plant.refreshedAt === null) return text.notRead;
    const hours = Math.floor(
        Math.max(0, format.now - plant.refreshedAt) / 3_600_000,
    );
    return text.read(format.device(plant.refreshedAt), text.age(hours));
}

function savedList(text, plants, format, heading) {
    if (plants.length === 0) return "";
    const items = plants.map(
        (plant) =>
            `<li>${link(plantLink(plant, "forecast"), plant.name)}
<p class="workspace">${escape(plant.workspace)}</p>
<p class="saved-at">${escape(readTime(text, plant, format))}</p></li>`,
    );
    return `${heading ? `<h2>${escape(text.savedPlants)}</h2>` : ""}
<ul class="saved-list" role="list">
${items.join("\n")}
</ul>
<p>${link("/app/?saved=remove", text.remove)}</p>
`;
}

function confirmation(text, token) {
    return `<h2>${escape(text.confirmHeading)}</h2>
<p>${escape(text.confirmText)}</p>
<p>${link(`/app/?saved=remove&token=${token}`, text.confirm)}</p>
<p>${link("/app/", text.keep)}</p>
`;
}

function chosenDay(days, address, now) {
    if (address.date) return days.find((day) => day.date === address.date);
    if (days.length === 0) return undefined;
    const zone = days.at(-1).timezone;
    const target = plantDay(zone, address.kind === "forecast" ? 1 : -1, now);
    return (
        days.find((day) => day.date === target) ??
        days.findLast((day) => day.date < target) ??
        days[0]
    );
}

function plantDay(timeZone, offset, now) {
    const today = Date.parse(`${localDate(timeZone)(now)}T00:00:00Z`);
    return new Date(today + offset * DAY_MS).toISOString().slice(0, 10);
}

function plantPage(language, plant, address, days, format) {
    const text = TEXTS[language];
    const { kind } = address;
    const day = chosenDay(days, address, format.now);
    const index = day ? days.indexOf(day) : -1;
    const neighbours = [
        index > 0 &&
            link(plantLink(plant, kind, days[index - 1].date), text.previous),
        index >= 0 &&
            index < days.length - 1 &&
            link(plantLink(plant, kind, days[index + 1].date), text.next),
    ].filter(Boolean);
    const views = ["forecast", "production"].map((view) =>
        view === kind
            ? `<span aria-current="page">${escape(text.kinds[view])}</span>`
            : link(plantLink(plant, view), text.kinds[view]),
    );
    const content = day
        ? kind === "forecast"
            ? forecastDay(text, plant, day, format, language)
            : productionDay(text, plant, day, format, language)
        : days.length > 0
          ? heldDays(text, plant, kind, days, format)
          : `<p>${escape(text.noDays)}</p>\n`;
    return `<!doctype html>
<html lang="${language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escape(`${plant.name}: ${text.kinds[kind]} | Pyracast`)}</title>
<style>
${STYLE}
</style>
</head>
<body>
<main class="plant">
<header class="plant-header">
<div class="back">${link("/app/", text.savedPlantsLink)}</div>
<p class="workspace">${escape(plant.workspace)}</p>
<h1>${escape(plant.name)}</h1>
</header>
<div class="saved-state">
<p class="saved-label"><strong>${escape(text.showingSaved)}</strong></p>
<p class="saved-at">${escape(readTime(text, plant, format))}</p>
<a href="" class="retry">${escape(text.tryAgain)}</a>
</div>
<nav class="view-links" aria-label="${escape(text.views)}">${views.join("")}</nav>
<section class="figures">
${dayHeader(text, day, kind, format, neighbours)}
<div class="day-content">
${content}</div>
</section>
<footer>${link("/app/?saved=remove", text.remove)}</footer>
</main>
</body>
</html>
`;
}

function dayHeader(text, day, kind, format, neighbours) {
    if (!day) return `<h2>${escape(text.kinds[kind])}</h2>`;
    return `<div class="day-header">
<div>
<h2><time datetime="${escape(day.date)}">${escape(format.day(day.date))}</time></h2>
<p class="day-meta">${escape(text.localTime)} · ${escape(day.timezone)}</p>
</div>
${neighbours.length ? `<nav class="day-navigation" aria-label="${escape(text.dayNavigation)}">${neighbours.join("")}</nav>` : ""}
</div>`;
}

function heldDays(text, plant, kind, days, format) {
    return `<p>${escape(text.dayNotSaved)}</p>
<h3>${escape(text.savedDays)}</h3>
<ul class="days">
${days.map((day) => `<li>${link(plantLink(plant, kind, day.date), format.day(day.date))}</li>`).join("\n")}
</ul>
`;
}

function label(labels, code) {
    return Object.hasOwn(labels, code) ? labels[code] : code;
}

function intervalFormat(language, day) {
    const offset = new Intl.DateTimeFormat(language, {
        timeZone: day.timezone,
        timeZoneName: "shortOffset",
    });
    const offsets = new Set(
        day.rows
            .flatMap((row) => [row.start, row.end])
            .map(
                (instant) =>
                    offset
                        .formatToParts(new Date(instant))
                        .find((part) => part.type === "timeZoneName")?.value,
            ),
    );
    const time = new Intl.DateTimeFormat(language, {
        timeZone: day.timezone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        ...(offsets.size > 1 ? { timeZoneName: "shortOffset" } : {}),
    });
    return (row) =>
        `${time.format(new Date(row.start))}–${time.format(new Date(row.end))}`;
}

function total(text, title, value, partial, format) {
    return `<p class="total"><span>${escape(title)}</span>
<strong>${escape(format.number(value))} <span>MWh</span></strong>
${partial ? `<small>${escape(text.partial)}</small>` : ""}</p>
`;
}

function forecastDay(text, plant, day, format, language) {
    const metadata = [
        text.source(label(text.sourceKinds, day.source_kind)),
        day.collected_at === null
            ? ""
            : text.collected(format.device(day.collected_at)),
    ].filter(Boolean);
    const notes = [
        day.source_kind === "mixed" ? text.mixed : "",
        day.stale_reason === null
            ? ""
            : label(text.staleReasons, day.stale_reason),
        day.affected && day.affected.status !== "estimated"
            ? label(text.impacts, day.affected.status)
            : "",
    ].filter(Boolean);
    const provenance = `<div class="meta">${metadata.map((value) => `<p>${escape(value)}</p>`).join("")}</div>
${notes.map((value) => `<p class="mark">${escape(value)}</p>`).join("")}`;
    if (day.unavailable_reason !== null) {
        return `${provenance}<p class="mark">${escape(label(text.reasons, day.unavailable_reason))}</p>`;
    }
    const affected = day.affected;
    const totals = `<div class="totals">
${total(text, text.total, day.total_mwh, day.partial, format)}
${affected ? total(text, text.affectedTotal, affected.total_mwh, affected.partial, format) : ""}
</div>`;
    if (day.rows.length === 0)
        return `${totals}${provenance}${emptyChart(text, day)}`;
    const interval = intervalFormat(language, day);
    const mixed = day.rows.some((row) => row.sources !== null);
    const head = [
        text.interval,
        text.energy,
        ...(affected ? [text.affected] : []),
        ...(mixed ? [text.sources] : []),
    ];
    const rows = day.rows.map((row) => {
        const cells = [
            `<td>${escape(interval(row))}</td>`,
            `<td class="number">${escape(format.number(row.mwh))}</td>`,
        ];
        if (affected)
            cells.push(
                `<td class="number">${escape(format.number(row.affected_mwh))}</td>`,
            );
        if (mixed)
            cells.push(
                `<td>${escape((row.sources ?? []).map((source) => label(text.sourceKinds, source)).join(", "))}</td>`,
            );
        return `<tr>${cells.join("")}</tr>`;
    });
    return `${totals}${provenance}${charts(text, plant, day, format, language, affected !== null)}${table(text, head, rows)}`;
}

function table(text, head, rows) {
    return `<details class="interval-values">
<summary>${escape(text.intervalValues)}</summary>
<div class="table" role="region" aria-label="${escape(text.intervals)}" tabindex="0">
<table>
<caption>${escape(text.intervals)}</caption>
<thead><tr>${head.map((cell) => `<th scope="col">${escape(cell)}</th>`).join("")}</tr></thead>
<tbody>
${rows.join("\n")}
</tbody>
</table>
</div>
</details>
`;
}

function emptyChart(text, day) {
    return `${day.daylight_windows?.length === 0 ? `<p>${escape(text.noDaylight)}</p>` : ""}<p>${escape(text.noRows)}</p>`;
}

function dayEdge(dateOf, date, low, high, start) {
    while (high - low > 1) {
        const mid = Math.floor((high + low) / 2);
        if ((dateOf(mid) === date) === start) high = mid;
        else low = mid;
    }
    return high;
}

function dayRange(day, rows) {
    const dateOf = localDate(day.timezone);
    const first = rows[0].start;
    const last = rows.at(-1).end - 1;
    // A sample within the day anchors each search even when local midnight is skipped.
    const margin = 2 * DAY_MS;
    return [
        dayEdge(dateOf, day.date, first - margin, first, true),
        dayEdge(dateOf, day.date, last, last + margin, false),
    ];
}

function focusRange(rows, daylight, full) {
    const positive = rows.filter((row) => row.mwh > 0 || row.affected_mwh > 0);
    if (daylight.length === 0) {
        const known = positive.length
            ? positive
            : rows.filter((row) => row.mwh != null || row.affected_mwh != null);
        return [known[0].start, known.at(-1).end];
    }
    const overlapping = rows.filter((row) =>
        daylight.some(({ start, end }) => row.start < end && row.end > start),
    );
    const shown = [...daylight, ...overlapping, ...positive];
    return [
        Math.max(full[0], Math.min(...shown.map((row) => row.start))),
        Math.min(full[1], Math.max(...shown.map((row) => row.end))),
    ];
}

function chartTicks(first, last, dayStart) {
    const hour = 3_600_000;
    const span = last - first;
    const step = Math.ceil(span / (4 * hour)) * hour;
    const ticks = [first];
    let tick = dayStart + (Math.floor((first - dayStart) / step) + 1) * step;
    for (; tick < last; tick += step) {
        if (tick - first >= span * 0.12 && last - tick >= span * 0.12)
            ticks.push(tick);
    }
    ticks.push(last);
    return ticks;
}

function charts(text, plant, day, format, language, affected) {
    const time = new Intl.DateTimeFormat(language, {
        timeZone: day.timezone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
    });
    const clock = (instant) => time.format(instant);
    const invalid = (row) =>
        day.kind === "production" && row.quality === "invalid";
    const rows = day.rows.map((row) => ({
        ...row,
        mwh: invalid(row) ? null : row.mwh,
        start: Date.parse(row.start),
        end: Date.parse(row.end),
    }));
    if (!rows.some((row) => row.mwh != null || row.affected_mwh != null))
        return `<p class="chart-note">${escape(text.invalidReadings)}</p>`;
    const daylight = (day.daylight_windows ?? []).map((window) => ({
        start: Date.parse(window.starts_at),
        end: daylightEnd(window.ends_at),
    }));
    const full = dayRange(day, rows);
    const top = Math.max(
        0,
        ...rows.flatMap((row) => [row.mwh ?? 0, row.affected_mwh ?? 0]),
    );
    const focus =
        day.daylight_windows?.length === 0 && top === 0
            ? null
            : focusRange(rows, daylight, full);
    const chart = (range) =>
        curve(text.curve(plant.name, format.day(day.date), affected), {
            rows,
            daylight,
            range,
            dayStart: full[0],
            top,
            number: format.number,
            clock,
            affected,
        });
    const zoomed = focus && (focus[0] !== full[0] || focus[1] !== full[1]);
    const plot =
        focus && !zoomed
            ? chart(full)
            : `<details class="full-day">
<summary>${escape(text.fullDay)}</summary>
${chart(full)}
</details>
${focus ? `<div class="focused-chart">${chart(focus)}</div>` : ""}`;
    const daylightNote = daylight.length
        ? `${text.daylight} · ${daylight.map(({ start, end }) => `${clock(start)}–${clock(end)}`).join(", ")}`
        : day.daylight_windows?.length === 0
          ? text.noDaylight
          : "";
    const notes = [
        daylightNote,
        day.rows.some(invalid) ? text.invalidReadings : "",
        top === 0 ? text.allZero : "",
        affected ? text.legend : "",
        text.marks,
    ].filter(Boolean);
    return `<div>
<strong class="chart-title">${escape(text.intervals)} (MWh)</strong>
${plot}
<div class="chart-note">${notes.map((note) => `<p>${escape(note)}</p>`).join("")}</div>
</div>
`;
}

function curve(
    label,
    { rows, daylight, range, dayStart, top, number, clock, affected },
) {
    const width = 600;
    const height = 180;
    const baseline = height - 1;
    const [first, last] = range;
    const point = (time) => ((time - first) / (last - first)) * width;
    const sun = daylight.map(({ start, end }) => {
        const startX = Math.max(0, point(start));
        const endX = Math.min(width, point(end));
        return endX > startX
            ? `<rect class="daylight" x="${startX.toFixed(2)}" y="0" width="${(endX - startX).toFixed(2)}" height="${height}"></rect>`
            : "";
    });
    const bars = rows.flatMap((row) => {
        if (row.start >= last || row.end <= first) return [];
        const start = point(row.start);
        const size = point(row.end) - start;
        const slot = affected ? size / 2 : size;
        const bar = (value, offset, kind) =>
            chartBar(
                value,
                start + offset + slot * 0.08,
                slot * 0.84,
                kind,
                baseline,
                top,
            );
        return [
            bar(row.mwh, 0, "energy"),
            affected ? bar(row.affected_mwh, slot, "affected") : "",
        ];
    });
    const ticks = chartTicks(first, last, dayStart);
    return `<div class="chart">
<div class="chart-y" aria-hidden="true"><span>${top > 0 ? escape(number(top)) : ""}</span><span>0</span></div>
<svg role="img" aria-label="${escape(label)}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
${sun.join("")}
${top > 0 ? `<line class="grid" x1="0" x2="${width}" y1="${height / 2}" y2="${height / 2}"></line>` : ""}
<line class="baseline" x1="0" x2="${width}" y1="${baseline}" y2="${baseline}"></line>
${bars.join("")}
</svg>
<div class="chart-time">${ticks.map((time) => `<span style="left:${((point(time) / width) * 100).toFixed(2)}%">${escape(clock(time))}</span>`).join("")}</div>
</div>
`;
}

function chartBar(value, x, width, kind, baseline, top) {
    if (value == null) return "";
    if (value === 0)
        return `<line class="zero zero-${kind}" x1="${x.toFixed(2)}" x2="${(x + width).toFixed(2)}" y1="${baseline}" y2="${baseline}"></line>`;
    const height = (value / top) * baseline;
    return `<rect class="${kind}" x="${x.toFixed(2)}" y="${(baseline - height).toFixed(2)}" width="${width.toFixed(2)}" height="${height.toFixed(2)}"></rect>`;
}

function productionDay(text, plant, day, format, language) {
    const lines = [
        total(text, text.total, day.total_mwh, false, format),
        `<p class="mark">${escape(text.complete[day.complete])}</p>\n`,
    ];
    if (day.rows.length === 0) {
        lines.push(emptyChart(text, day));
        return lines.join("");
    }
    const interval = intervalFormat(language, day);
    const rows = day.rows.map(
        (row) =>
            `<tr><td>${escape(interval(row))}</td><td class="number">${escape(format.number(row.mwh))}</td><td>${escape(row.quality === null ? "—" : label(text.qualities, row.quality))}</td><td>${escape(row.source ?? "—")}</td><td>${escape(row.provisional ? text.provisional : "—")}</td></tr>`,
    );
    lines.push(
        charts(text, plant, day, format, language, false),
        table(
            text,
            [
                text.interval,
                text.energy,
                text.quality,
                text.productionSource,
                text.mark,
            ],
            rows,
        ),
    );
    return lines.join("");
}
