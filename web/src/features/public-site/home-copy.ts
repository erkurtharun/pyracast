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
        signup: "Create a workspace",
        skip: "Skip to content",
        privacy: "Privacy",
        terms: "Terms",
        contact: "Contact",
        footer: "A clearer view of solar production.",
        eyebrow: "Solar production forecasting",
        title: "Plan tomorrow’s solar production.",
        intro: "Bring your plant forecasts, reported production and operating records together. Give your team the information to plan the day ahead.",
        secondary: "Explore the product",
        chartTitle: "Daily production",
        chartAxis: "Power",
        chartDescription:
            "A schematic daily power profile, with a forecast curve and reported production through the morning.",
        forecast: "Forecast",
        actual: "Reported production",
        chartNote:
            "Illustrative daily profile showing forecast and reported production.",
        productEyebrow: "The plant workspace",
        productTitle: "Forecasts, production and operations in one place.",
        features: [
            {
                title: "See what’s ahead",
                text: "Review expected generation across the day and daily energy estimates in your plant’s timezone.",
            },
            {
                title: "Compare with production",
                text: "Review available production alongside forecasts, with missing intervals kept visible.",
            },
            {
                title: "Keep your team informed",
                text: "Share plant information, record outages and schedule forecast emails with Excel reports.",
            },
        ],
        startEyebrow: "Getting started",
        startTitle: "Start with your plant.",
        startText:
            "Create a workspace and add your plant details. Forecasts become available once the setup has been reviewed and approved.",
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
        eyebrow: "Güneş üretim tahmini",
        title: "Yarının üretimini bugünden planlayın.",
        intro: "Santral tahminlerini, gerçekleşen üretimi ve operasyon kayıtlarını bir araya getirin. Ekibinizle bir sonraki güne hazırlanın.",
        secondary: "Ürünü inceleyin",
        chartTitle: "Günlük üretim",
        chartAxis: "Güç",
        chartDescription:
            "Günlük güç profilinde tahmin eğrisini ve sabah saatlerinde gerçekleşen üretimi gösteren şema.",
        forecast: "Tahmin",
        actual: "Gerçekleşen üretim",
        chartNote:
            "Tahmini ve gerçekleşen üretimi gösteren temsili günlük profil.",
        productEyebrow: "Santral çalışma alanı",
        productTitle: "Tahmin, üretim ve operasyon aynı yerde.",
        features: [
            {
                title: "Üretimi öngörün",
                text: "Gün içindeki beklenen üretimi ve günlük enerji tahminini santralin saat diliminde inceleyin.",
            },
            {
                title: "Gerçekleşen üretimle karşılaştırın",
                text: "Mevcut üretim verilerini tahminlerle birlikte değerlendirin; eksik aralıkları görün.",
            },
            {
                title: "Ekibinizle paylaşın",
                text: "Santral bilgilerini paylaşın, kesintileri kaydedin ve Excel raporlu tahmin e-postalarını planlayın.",
            },
        ],
        startEyebrow: "Başlangıç",
        startTitle: "Santralinizle başlayın.",
        startText:
            "Çalışma alanınızı oluşturun ve santral bilgilerinizi ekleyin. Kurulum incelenip onaylandıktan sonra tahminler kullanıma açılır.",
        updated: "Son güncelleme",
        contents: "Bu sayfada",
        back: "Ana sayfaya dön",
    },
};
