export type PublicLanguage = "en" | "tr";

type HomeCopy = {
    nav: string;
    stepsNav: string;
    faqNav: string;
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
    approval: string;
    illustration: string;
    chartTitle: string;
    chartSubtitle: string;
    chartAxis: string;
    forecast: string;
    actual: string;
    chartNote: string;
    previewLabel: string;
    benefitsEyebrow: string;
    benefitsTitle: string;
    benefits: readonly { title: string; text: string }[];
    stepsEyebrow: string;
    stepsTitle: string;
    steps: readonly { title: string; text: string }[];
    faqTitle: string;
    faq: readonly { question: string; answer: string }[];
    ctaTitle: string;
    ctaText: string;
    updated: string;
    contents: string;
    back: string;
};

export const homeCopy: Record<PublicLanguage, HomeCopy> = {
    en: {
        nav: "Product",
        stepsNav: "How it works",
        faqNav: "Questions",
        login: "Sign in",
        signup: "Create a workspace",
        skip: "Skip to content",
        privacy: "Privacy",
        terms: "Terms",
        contact: "Contact",
        footer: "A clearer view of solar production.",
        eyebrow: "Solar production forecasting",
        title: "See tomorrow’s solar production, today.",
        intro: "Bring your plant forecasts, reported production and operating records together. Give your team a clear view of what comes next.",
        secondary: "See how it works",
        approval:
            "Forecasts become available after plant details and authorized data connections are approved.",
        illustration: "Illustrative product preview",
        chartTitle: "A day in view",
        chartSubtitle: "Solar production · illustrative curve",
        chartAxis: "Power",
        forecast: "Forecast",
        actual: "Reported production",
        chartNote:
            "Illustrative data. Not a live forecast or an accuracy comparison.",
        previewLabel: "From forecast to daily planning",
        benefitsEyebrow: "One connected workspace",
        benefitsTitle: "Forecasts and production. One clear view.",
        benefits: [
            {
                title: "Follow production forecasts",
                text: "Review expected generation across the day, with daily energy estimates and the plant’s own timezone.",
            },
            {
                title: "Compare with reported production",
                text: "Bring forecasts and available production observations into the same view, with their sources and missing intervals visible.",
            },
            {
                title: "Keep your team in context",
                text: "Share plant information, track outages and schedule forecast emails with Excel reports. Keep access under your control.",
            },
        ],
        stepsEyebrow: "A considered start",
        stepsTitle: "Your plant. Your data. A clear setup.",
        steps: [
            {
                title: "Create your workspace",
                text: "Register your company’s workspace, verify your email and invite the people who need access.",
            },
            {
                title: "Add your plant",
                text: "Submit the location and technical details that define your plant, including its declared capacities.",
            },
            {
                title: "Start with an approved connection",
                text: "An administrator reviews the setup and connects an authorized provider account before forecasts are collected.",
            },
        ],
        faqTitle: "A few things to know.",
        faq: [
            {
                question: "Who is Pyracast for?",
                answer: "Pyracast is built for teams that need to follow solar plant production forecasts and keep plant information, observations and operations together.",
            },
            {
                question: "Will forecasts appear as soon as I register?",
                answer: "Registration creates a workspace. Forecasts require submitted plant details and an approved, authorized provider connection. Provider access and availability depend on the approved setup.",
            },
            {
                question: "Are forecasts guaranteed production?",
                answer: "No. Forecasts are estimates based on available source data and plant configuration. Weather, operating conditions and incomplete inputs can affect results. Missing data is kept visible.",
            },
            {
                question: "Can I use it in Turkish or English?",
                answer: "Yes. The workspace supports Turkish and English. Plant timezones, physical units and source records keep their meaning whichever language you choose.",
            },
        ],
        ctaTitle: "Give your next day a clearer outlook.",
        ctaText:
            "Start with your workspace. Build the picture around your plants.",
        updated: "Last updated",
        contents: "On this page",
        back: "Back to home",
    },
    tr: {
        nav: "Ürün",
        stepsNav: "Nasıl çalışır?",
        faqNav: "Sorular",
        login: "Giriş yap",
        signup: "Çalışma alanı oluştur",
        skip: "İçeriğe geç",
        privacy: "Gizlilik",
        terms: "Kullanım koşulları",
        contact: "İletişim",
        footer: "Güneş üretimine daha net bir bakış.",
        eyebrow: "Güneş üretim tahmini",
        title: "Yarının güneş üretimini bugünden görün.",
        intro: "Santral tahminlerini, gerçekleşen üretimi ve operasyon kayıtlarını bir araya getirin. Ekibinizle bir sonraki güne daha net bakın.",
        secondary: "Nasıl çalıştığını görün",
        approval:
            "Tahminler, santral bilgileri ve yetkili veri bağlantısı onaylandıktan sonra kullanılabilir.",
        illustration: "Temsili ürün görünümü",
        chartTitle: "Bir güne bakış",
        chartSubtitle: "Güneş üretimi · temsili eğri",
        chartAxis: "Güç",
        forecast: "Tahmin",
        actual: "Gerçekleşen üretim",
        chartNote:
            "Temsili veridir. Canlı tahmin veya doğruluk karşılaştırması değildir.",
        previewLabel: "Tahminden günlük planlamaya",
        benefitsEyebrow: "Tek çalışma alanı",
        benefitsTitle: "Tahmin ve üretim aynı yerde.",
        benefits: [
            {
                title: "Üretim tahminlerini izleyin",
                text: "Günlük enerji tahminlerini ve gün içindeki beklenen üretimi, santralinizin kendi saat diliminde inceleyin.",
            },
            {
                title: "Gerçekleşen üretimle karşılaştırın",
                text: "Tahminleri ve mevcut üretim verilerini aynı görünümde değerlendirin. Kaynakları ve eksik zaman aralıklarını görün.",
            },
            {
                title: "Ekibiniz aynı bilgiye baksın",
                text: "Santral bilgilerini paylaşın, kesintileri kaydedin ve Excel raporlu tahmin e-postalarını planlayın. Kimlerin erişebileceğini siz belirleyin.",
            },
        ],
        stepsEyebrow: "Adım adım başlangıç",
        stepsTitle: "Santraliniz. Verileriniz. Net bir başlangıç.",
        steps: [
            {
                title: "Çalışma alanınızı oluşturun",
                text: "Şirketinizin çalışma alanını oluşturun, e-postanızı doğrulayın ve erişmesi gereken ekip üyelerini davet edin.",
            },
            {
                title: "Santralinizi ekleyin",
                text: "Santralin konumunu, beyan edilen kapasitelerini ve teknik özelliklerini gönderin.",
            },
            {
                title: "Onaylanan bağlantıyla başlayın",
                text: "Yönetici, yapılandırmayı inceler ve tahmin verileri alınmadan önce yetkili bir sağlayıcı hesabını bağlar.",
            },
        ],
        faqTitle: "Başlamadan önce.",
        faq: [
            {
                question: "Pyracast kimler için?",
                answer: "Pyracast, güneş santrallerinin üretim tahminlerini takip etmek; santral bilgilerini, üretim verilerini ve operasyon kayıtlarını bir arada tutmak isteyen ekipler için geliştirilmiştir.",
            },
            {
                question: "Kayıt olur olmaz tahminleri görebilir miyim?",
                answer: "Kayıt işlemi bir çalışma alanı oluşturur. Tahminlerin başlaması için santral bilgilerinin gönderilmesi ve yetkili sağlayıcı bağlantısının onaylanması gerekir. Sağlayıcı erişimi ve veri kullanılabilirliği onaylanan yapılandırmaya bağlıdır.",
            },
            {
                question: "Tahminler üretim garantisi verir mi?",
                answer: "Hayır. Tahminler, mevcut kaynak verileri ve santral yapılandırmasına dayanan öngörülerdir. Hava koşulları, işletme durumu ve eksik girdiler sonuçları etkileyebilir. Eksik veriler görünür tutulur.",
            },
            {
                question: "Türkçe ve İngilizce kullanabilir miyim?",
                answer: "Evet. Çalışma alanı Türkçe ve İngilizceyi destekler. Seçtiğiniz dil değişse de santralin saat dilimi, fiziksel birimler ve kaynak kayıtları anlamını korur.",
            },
        ],
        ctaTitle: "Bir sonraki güne daha net bakın.",
        ctaText:
            "Çalışma alanınızla başlayın. Santrallerinizin görünümünü birlikte oluşturun.",
        updated: "Son güncelleme",
        contents: "Bu sayfada",
        back: "Ana sayfaya dön",
    },
};
