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

export const homeCopy: Record<PublicLanguage, HomeCopy> = {
    en: {
        nav: "Product",
        startNav: "Getting started",
        login: "Sign in",
        signup: "Create workspace",
        skip: "Skip to content",
        privacy: "Privacy",
        terms: "Terms",
        contact: "Contact",
        footer: "A clearer view of solar production.",
        eyebrow: "Solar generation forecasts",
        title: "Plan tomorrow’s solar production.",
        intro: "Bring generation forecasts, actual production and outages of your plants together. Give your team the information to plan the day ahead.",
        secondary: "Explore the product",
        chartTitle: "Daily production",
        chartAxis: "Power",
        chartDescription:
            "A schematic daily power profile, with a forecast curve and actual production through the morning.",
        forecast: "Forecast",
        actual: "Actual production",
        chartNote:
            "Illustrative daily profile of forecast and actual production.",
        productEyebrow: "The plant workspace",
        productTitle: "Forecasts, production and operations in one place.",
        features: [
            {
                title: "Generation forecast for tomorrow",
                text: "See the generation forecast through the day and its daily total, in plant time.",
            },
            {
                title: "Compare with actual production",
                text: "See actual production beside the forecast, with missing hours kept visible.",
            },
            {
                title: "Keep your team informed",
                text: "Share plant information, record outages and send forecast emails with an Excel file.",
            },
        ],
        startEyebrow: "Getting started",
        startTitle: "Start with your plant.",
        startText:
            "Create a workspace and add your plant. Forecasts start once Pyracast approves the plant.",
        updated: "Last updated",
        contents: "On this page",
        back: "Back to home",
    },
    tr: {
        nav: "Ürün",
        startNav: "Başlangıç",
        login: "Giriş yap",
        signup: "Çalışma alanı oluştur",
        skip: "İçeriğe geç",
        privacy: "Gizlilik",
        terms: "Kullanım koşulları",
        contact: "İletişim",
        footer: "Güneş üretimine daha net bir bakış.",
        eyebrow: "Güneş santralleri için üretim tahmini",
        title: "Yarının üretimini bugünden planlayın.",
        intro: "Santrallerinizin üretim tahminlerini, gerçekleşen üretimini ve kesintilerini bir araya getirin. Ekibinizle bir sonraki güne hazırlanın.",
        secondary: "Ürünü inceleyin",
        chartTitle: "Günlük üretim",
        chartAxis: "Güç",
        chartDescription:
            "Günlük güç profilinde tahmin eğrisini ve sabah saatlerinde gerçekleşen üretimi gösteren şema.",
        forecast: "Tahmin",
        actual: "Gerçekleşen üretim",
        chartNote:
            "Tahmin ve gerçekleşen üretimi gösteren temsili günlük profil.",
        productEyebrow: "Santral çalışma alanı",
        productTitle: "Tahmin, üretim ve operasyon aynı yerde.",
        features: [
            {
                title: "Yarın için üretim tahmini",
                text: "Gün boyunca üretim tahminini ve günlük toplamı santral saatiyle görün.",
            },
            {
                title: "Gerçekleşen üretimle karşılaştırın",
                text: "Gerçekleşen üretimi tahminle yan yana görün; eksik saatler görünür kalır.",
            },
            {
                title: "Ekibinizle paylaşın",
                text: "Santral bilgilerini paylaşın, kesintileri kaydedin ve Excel dosyası ekli tahmin e-postaları gönderin.",
            },
        ],
        startEyebrow: "Başlangıç",
        startTitle: "Santralinizle başlayın.",
        startText:
            "Çalışma alanınızı oluşturun ve santralinizi ekleyin. Pyracast santrali onayladığında tahminler başlar.",
        updated: "Son güncelleme",
        contents: "Bu sayfada",
        back: "Ana sayfaya dön",
    },
};
