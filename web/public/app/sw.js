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
        showingSaved: "Showing the figures saved on this device.",
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
        plantDay: (day, zone) => `Plant day ${day}, time zone ${zone}.`,
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
        total: (value) => `Day total: ${value} MWh`,
        affectedTotal: (value) => `With outages: ${value} MWh`,
        partial: "partial estimate",
        impacts: { unknown_impact: "Outage impact is unknown." },
        curve: (plant, day, affected) =>
            `Energy per interval of ${plant} on ${day}${affected ? ", at normal capacity and with outages" : ""}. The table below gives the values; missing values are gaps.`,
        legend: "In each interval the left bar is normal capacity and the right bar is with outages.",
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
        showingSaved: "Bu cihazda kayıtlı rakamlar gösteriliyor.",
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
        plantDay: (day, zone) => `Santral günü ${day}, saat dilimi ${zone}.`,
        source: (kind) => `Kaynak: ${kind}.`,
        sourceKinds: {
            forecast: "Öngörü",
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
        total: (value) => `Gün toplamı: ${value} MWh`,
        affectedTotal: (value) => `Kesintiler dahil: ${value} MWh`,
        partial: "kısmi tahmin",
        impacts: { unknown_impact: "Kesintinin etkisi bilinmiyor." },
        curve: (plant, day, affected) =>
            `${plant} santralinin ${day} günü aralık başına enerjisi${affected ? ", normal kapasitede ve kesintiler dahil" : ""}. Değerler aşağıdaki tabloda; eksik değerler boşluk olarak gösterilir.`,
        legend: "Her aralıkta soldaki çubuk normal kapasiteyi, sağdaki çubuk kesintiler dahil değeri gösterir.",
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

const STYLE = `body { margin: 0; font-family: system-ui, sans-serif; background: #f6f7f9; color: #1d2430; }
main { max-width: 48rem; margin: 10vh auto; padding: 0 1rem; overflow-wrap: anywhere; }
section + section { margin-top: 2rem; padding-top: 2rem; border-top: 1px solid #d6dae1; }
a { color: inherit; display: inline-block; min-height: 1.5rem; line-height: 1.5rem; }
li + li { margin-top: 0.25rem; }
nav ul, .days { padding-left: 1.25rem; }
.figures { margin-top: 2rem; }
.mark { font-weight: 600; }
svg { display: block; width: 100%; height: 10rem; margin: 1rem 0; background: #ffffff; border: 1px solid #d6dae1; }
.energy { fill: #2f6fb3; }
.affected { fill: #c2581b; }
.table { overflow-x: auto; margin-top: 1rem; }
table { border-collapse: collapse; width: 100%; overflow-wrap: break-word; }
caption { text-align: left; font-weight: 600; margin-bottom: 0.5rem; }
th, td { padding: 0.25rem 0.5rem; border-bottom: 1px solid #d6dae1; text-align: left; }
td.number { text-align: right; font-variant-numeric: tabular-nums; }
@media (prefers-color-scheme: dark) {
body { background: #11151c; color: #e6e9ef; }
svg { background: #1b2129; border-color: #3a4350; }
th, td { border-bottom-color: #3a4350; }
.energy { fill: #7fb2ea; }
.affected { fill: #f09a62; }
}`;

const NOTICE = noticePage({ languages: LANGUAGES });

function escape(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function noticePage({ languages, title, body }) {
    const sections = languages.map((language) => {
        const text = TEXTS[language];
        return `<section lang="${language}">
<h1>${escape(text.title)}</h1>
<p>${escape(text.unreachable)}</p>
<p><a href="">${escape(text.tryAgain)}</a></p>
<p>${escape(text.invitation)}</p>
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
        held: 0,
        plants: [],
        plant: undefined,
        days: [],
        outcome: undefined,
        readAt: undefined,
    };
    const holder = transaction.objectStore("holder").get("holder");
    holder.onsuccess = () => {
        copy.holder = holderOf(holder.result);
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

function dayOf(value, key, plant, kind) {
    return matches(value, {
        tenant: (tenant) => tenant === plant.tenant,
        site: (site) => site === plant.site,
        plant: (id) => id === plant.plant,
        kind: (stored) => stored === kind,
        date: isDate,
        timezone: isZone,
        ...(kind === "forecast" ? FORECAST_DAY : PRODUCTION_DAY),
    }) &&
        sameKey(key, [
            plant.tenant,
            plant.site,
            plant.plant,
            kind,
            value.date,
        ]) &&
        (kind === "production" || forecastFits(value)) &&
        onDay(value)
        ? value
        : undefined;
}

function buildPage(copy, address, { now, zone, outcome }) {
    const holder = copy.holder;
    if (!holder) return NOTICE;
    const languages = [holder.language];
    const sentence = (key) => (text) => `<p>${escape(text[key])}</p>\n`;
    if (outcome) return noticePage({ languages, body: sentence(outcome) });
    if (!holder.active || expired(holder, now))
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
    return noticePage({
        languages,
        body: (text) => savedList(text, plants, format),
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

function savedList(text, plants, format) {
    if (plants.length === 0) return "";
    const items = plants.map(
        (plant) =>
            `<li>${link(plantLink(plant, "forecast"), plant.name)}, ${escape(plant.workspace)}. ${escape(readTime(text, plant, format))}</li>`,
    );
    return `<h2>${escape(text.savedPlants)}</h2>
<ul>
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
    const kind = address.kind;
    const other = kind === "forecast" ? "production" : "forecast";
    const day = chosenDay(days, address, format.now);
    const index = day ? days.indexOf(day) : -1;
    const neighbours = [
        index > 0 &&
            link(plantLink(plant, kind, days[index - 1].date), text.previous),
        index >= 0 &&
            index < days.length - 1 &&
            link(plantLink(plant, kind, days[index + 1].date), text.next),
        link(plantLink(plant, other), text.kinds[other]),
        link("/app/", text.savedPlantsLink),
        link("/app/?saved=remove", text.remove),
    ].filter(Boolean);
    const content = day
        ? kind === "forecast"
            ? forecastDay(text, plant, day, format, language)
            : productionDay(text, day, format, language)
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
<main>
<h1>${escape(text.title)}</h1>
<p>${escape(text.unreachable)} ${escape(text.showingSaved)}</p>
<p>${escape(readTime(text, plant, format))}</p>
<p><a href="">${escape(text.tryAgain)}</a></p>
<nav aria-label="${escape(text.savedPlants)}">
<ul>
${neighbours.map((item) => `<li>${item}</li>`).join("\n")}
</ul>
</nav>
<section class="figures">
<h2>${escape(`${plant.name}, ${plant.workspace}: ${text.kinds[kind]}`)}</h2>
${content}</section>
</main>
</body>
</html>
`;
}

function heldDays(text, plant, kind, days, format) {
    return `<p>${escape(text.dayNotSaved)}</p>
<h3>${escape(text.savedDays)}</h3>
<ul class="days">
${days.map((day) => `<li>${link(plantLink(plant, kind, day.date), format.day(day.date))}</li>`).join("\n")}
</ul>
`;
}

function dayHeading(text, day, format) {
    return `<h3>${escape(format.day(day.date))}</h3>
<p>${escape(text.plantDay(day.date, day.timezone))}</p>
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

function total(text, line, value, partial, format) {
    return `<p>${escape(line(format.number(value)))}${partial ? ` (${escape(text.partial)})` : ""}</p>\n`;
}

function forecastDay(text, plant, day, format, language) {
    const lines = [
        dayHeading(text, day, format),
        `<p>${escape(text.source(label(text.sourceKinds, day.source_kind)))}</p>\n`,
    ];
    if (day.source_kind === "mixed")
        lines.push(`<p class="mark">${escape(text.mixed)}</p>\n`);
    if (day.collected_at !== null)
        lines.push(
            `<p>${escape(text.collected(format.device(day.collected_at)))}</p>\n`,
        );
    if (day.stale_reason !== null)
        lines.push(
            `<p class="mark">${escape(label(text.staleReasons, day.stale_reason))}</p>\n`,
        );
    if (day.unavailable_reason !== null) {
        lines.push(
            `<p class="mark">${escape(label(text.reasons, day.unavailable_reason))}</p>\n`,
        );
        return lines.join("");
    }
    lines.push(total(text, text.total, day.total_mwh, day.partial, format));
    if (day.affected) {
        lines.push(
            total(
                text,
                text.affectedTotal,
                day.affected.total_mwh,
                day.affected.partial,
                format,
            ),
        );
        if (day.affected.status !== "estimated")
            lines.push(
                `<p class="mark">${escape(label(text.impacts, day.affected.status))}</p>\n`,
            );
    }
    if (day.rows.length === 0) {
        lines.push(`<p>${escape(text.noRows)}</p>\n`);
        return lines.join("");
    }
    const interval = intervalFormat(language, day);
    const affected = day.affected !== null;
    const mixed = day.rows.some((row) => row.sources !== null);
    lines.push(curve(text, plant, day, format, affected));
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
    lines.push(table(text.intervals, head, rows));
    return lines.join("");
}

function table(caption, head, rows) {
    return `<div class="table" role="region" aria-label="${escape(caption)}" tabindex="0">
<table>
<caption>${escape(caption)}</caption>
<thead><tr>${head.map((cell) => `<th scope="col">${escape(cell)}</th>`).join("")}</tr></thead>
<tbody>
${rows.join("\n")}
</tbody>
</table>
</div>
`;
}

function curve(text, plant, day, format, affected) {
    const width = 600;
    const height = 160;
    const slot = width / day.rows.length;
    const values = day.rows.flatMap((row) => [row.mwh, row.affected_mwh]);
    const top = Math.max(0, ...values.filter((value) => value !== null));
    const bar = (value, x, kind) => {
        if (value === null) return "";
        const size = top > 0 ? (Math.max(0, value) / top) * height : 0;
        return `<rect class="${kind}" x="${x.toFixed(2)}" y="${(height - size).toFixed(2)}" width="${(affected ? slot / 2 : slot).toFixed(2)}" height="${size.toFixed(2)}"></rect>`;
    };
    const bars = day.rows.flatMap((row, index) => [
        bar(row.mwh, index * slot, "energy"),
        affected
            ? bar(row.affected_mwh, index * slot + slot / 2, "affected")
            : "",
    ]);
    const legend = affected ? `<p>${escape(text.legend)}</p>\n` : "";
    return `<svg role="img" aria-label="${escape(text.curve(plant.name, format.day(day.date), affected))}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
${bars.filter(Boolean).join("\n")}
</svg>
${legend}`;
}

function productionDay(text, day, format, language) {
    const lines = [
        dayHeading(text, day, format),
        `<p class="mark">${escape(text.complete[day.complete])}</p>\n`,
        total(text, text.total, day.total_mwh, false, format),
    ];
    if (day.rows.length === 0) {
        lines.push(`<p>${escape(text.noRows)}</p>\n`);
        return lines.join("");
    }
    const interval = intervalFormat(language, day);
    const rows = day.rows.map(
        (row) =>
            `<tr><td>${escape(interval(row))}</td><td class="number">${escape(format.number(row.mwh))}</td><td>${escape(row.quality === null ? "—" : label(text.qualities, row.quality))}</td><td>${escape(row.source ?? "—")}</td><td>${escape(row.provisional ? text.provisional : "—")}</td></tr>`,
    );
    lines.push(
        table(
            text.intervals,
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
