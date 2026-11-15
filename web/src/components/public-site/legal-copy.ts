type Language = "en" | "tr";
type LegalKind = "privacy" | "terms";

type PublicOperator = {
    name: string;
    address: string;
    country: string;
    email: string;
};

export type LegalSection = {
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

const googleDataPolicy =
    "https://developers.google.com/terms/api-services-user-data-policy";
const operatorDetails = {
    name: "HARUN ERKURT",
    address: { en: "Istanbul", tr: "İstanbul" },
    country: "Türkiye",
    email: "info@pyracast.app",
};

function privacyEnglish(operator: PublicOperator): LegalContent {
    return {
        kind: "privacy",
        title: "Privacy policy",
        intro: "How Pyracast handles your account and solar forecasting data.",
        updated: "2026-11-15",
        sections: [
            {
                id: "operator",
                title: "Who operates Pyracast",
                paragraphs: [
                    `${operator.name} is the data controller operating Pyracast. Location: ${operator.address}, ${operator.country}. Contact: ${operator.email}.`,
                ],
            },
            {
                id: "information",
                title: "Information we handle",
                paragraphs: [
                    "We collect information electronically through forms, connected services and automatic security records, from you, workspace administrators and service providers.",
                ],
                bullets: [
                    "Account identifiers, email, password hashes where used, preferences, membership and security settings.",
                    "Workspace and plant details, locations, production, forecasts, incidents and change histories.",
                    "Recipient addresses, schedules, messages and delivery records.",
                    "Sessions, IP addresses, authentication and operational logs.",
                ],
            },
            {
                id: "purposes",
                title: "Why we use it",
                paragraphs: [
                    "We use information to manage access, provide forecasts, send service messages and requested reminders, support users and investigate failures or abuse.",
                    "Processing relies on contract performance where applicable, our legitimate interest in service security subject to your rights, and applicable legal obligations.",
                ],
            },
            {
                id: "sign-in",
                title: "Google and Microsoft sign-in",
                paragraphs: [
                    "If you choose Google or Microsoft sign-in, we receive identity, email and basic profile information to authenticate or link your account. We retain the identifier and email, without sign-in access or refresh tokens. Sign-in gives no mailbox access.",
                    "Disconnect in Account → Security once another sign-in method works. Disconnecting does not delete account or workspace records.",
                ],
                links: [
                    {
                        label: "Google API Services User Data Policy",
                        href: googleDataPolicy,
                    },
                ],
            },
            {
                id: "sharing",
                title: "Who receives information",
                paragraphs: [
                    "Workspace members access shared records according to their roles; the operator accesses them for administration and support. Selected recipients receive configured messages.",
                    "Hosting, traffic security, email, sign-in and forecast or production data providers process information needed for those functions, including relevant plant identifiers and coordinates. Processing may occur abroad. Authorities may receive legally required disclosures.",
                ],
            },
            {
                id: "storage",
                title: "Cookies and browser storage",
                paragraphs: [
                    "Cookies and browser storage support secure sign-in, invitations and language preferences. The application includes no advertising or behavioral analytics trackers. You can clear browser data; this may end sessions or reset preferences.",
                ],
            },
            {
                id: "retention",
                title: "Retention and deletion",
                paragraphs: [
                    "Records are kept as needed for the service, shared workspaces, security and legal obligations. Deleted plants and locations have a 30-day restore period; backups are configured to expire after seven days. Cleanup is not immediate across all copies.",
                    `Request account deletion at ${operator.email}. We may verify identity and workspace ownership. Removing membership or connections preserves shared records. We explain any required retention.`,
                ],
            },
            {
                id: "rights",
                title: "Your questions and rights",
                paragraphs: [
                    `Contact ${operator.email} to learn whether and how your data is processed, its purposes, proper use and recipients, or request access, correction or deletion. Applicable rights include recipient notification of corrections or deletions, objections to adverse automated decisions, compensation for unlawful processing and complaints to authorities. KVKK requests are concluded within 30 days.`,
                    "We update this dated notice when practices change.",
                ],
            },
        ],
    };
}

function privacyTurkish(operator: PublicOperator): LegalContent {
    return {
        kind: "privacy",
        title: "Gizlilik politikası",
        intro: "Pyracast hesap bilgilerinizi ve güneş enerjisi tahmin verilerini nasıl işler?",
        updated: "2026-11-15",
        sections: [
            {
                id: "operator",
                title: "Pyracast'i kim işletiyor?",
                paragraphs: [
                    `${operator.name}, Pyracast'i işleten veri sorumlusudur. Konum: ${operator.address}, ${operator.country}. İletişim: ${operator.email}.`,
                ],
            },
            {
                id: "information",
                title: "İşlediğimiz bilgiler",
                paragraphs: [
                    "Bilgileri sizden, çalışma alanı yöneticilerinden ve hizmet sağlayıcılarından; elektronik formlar, bağlı hizmetler ve otomatik güvenlik kayıtları yoluyla toplarız.",
                ],
                bullets: [
                    "Hesap kimlikleri, e-posta, varsa parola özetleri, tercihler, üyelik ve güvenlik ayarları.",
                    "Çalışma alanı ve santral bilgileri, konumlar, üretim, tahminler, olaylar ve değişiklik geçmişi.",
                    "Alıcı adresleri, programlar, mesajlar ve gönderim kayıtları.",
                    "Oturumlar, IP adresleri, kimlik doğrulama kayıtları ve işletim günlükleri.",
                ],
            },
            {
                id: "purposes",
                title: "Neden kullanıyoruz?",
                paragraphs: [
                    "Bilgileri erişimi yönetmek, tahmin sunmak, hizmet mesajları ve talep edilen hatırlatmaları göndermek, destek sağlamak ve hataları veya kötüye kullanımı incelemek için kullanırız.",
                    "İşlemenin hukuki sebepleri, uygulanabildiği ölçüde sözleşmenin ifası, haklarınız gözetilerek hizmet güvenliğini sağlamaya yönelik meşru menfaatimiz ve hukuki yükümlülüklerimizdir.",
                ],
            },
            {
                id: "sign-in",
                title: "Google ve Microsoft ile giriş",
                paragraphs: [
                    "Google veya Microsoft ile giriş yaparsanız, girişinizi doğrulamak veya hesabınızı bağlamak için kimlik, e-posta ve temel profil bilgilerini alırız. Sağlayıcı kimliğini ve e-postayı saklarız; giriş için kullanılan erişim veya yenileme belirteçlerini saklamayız. Giriş yapmak posta kutunuza erişim vermez.",
                    "Başka bir giriş yöntemi çalışır durumdaysa Hesap → Güvenlik bölümünden bağlantıyı kaldırabilirsiniz. Bu işlem hesabınızı veya çalışma alanı kayıtlarını silmez.",
                ],
                links: [
                    {
                        label: "Google API Hizmetleri Kullanıcı Verileri Politikası",
                        href: googleDataPolicy,
                    },
                ],
            },
            {
                id: "sharing",
                title: "Bilgiler kimlere aktarılır?",
                paragraphs: [
                    "Çalışma alanı üyeleri rollerinin izin verdiği kayıtlara erişir. İşletmeci hizmet yönetimi ve destek için kayıtlara erişir. Seçilen alıcılar, ayarlanan mesajları alır.",
                    "Barındırma, trafik güvenliği, e-posta, giriş ve tahmin veya üretim verisi hizmetleri sunan sağlayıcılar, ilgili işlevler için gereken bilgileri işler; bunlara ilgili santral kimlikleri ve koordinatları da dahildir. İşlemler yurt dışında da gerçekleşebilir. Kanunen gerekli bilgiler yetkili mercilerle paylaşılabilir.",
                ],
            },
            {
                id: "storage",
                title: "Çerezler ve tarayıcı depolaması",
                paragraphs: [
                    "Çerezler ve tarayıcı depolaması; güvenli giriş, davetler ve dil tercihleri için kullanılır. Uygulamada reklam veya davranış analizi izleyicileri bulunmaz. Tarayıcı verilerini silebilirsiniz; bu işlem oturumunuzu kapatabilir veya tercihlerinizi sıfırlayabilir.",
                ],
            },
            {
                id: "retention",
                title: "Saklama ve silme",
                paragraphs: [
                    "Kayıtlar; hizmet, paylaşılan çalışma alanları, güvenlik ve hukuki yükümlülükler için gerektiği sürece saklanır. Silinen santral ve konumlar 30 gün içinde geri yüklenebilir; yedekler yedi gün sonra temizlenecek şekilde ayarlanır. Tüm kopyalardan silme anında gerçekleşmez.",
                    `Hesap silme talebinizi ${operator.email} adresine iletin. Kimliğinizi ve çalışma alanı sahipliğini doğrulamamız gerekebilir. Üyeliği veya bağlantıları kaldırmak ortak kayıtları silmez. Saklanması gereken bilgilerin nedenlerini açıklarız.`,
                ],
            },
            {
                id: "rights",
                title: "Sorularınız ve haklarınız",
                paragraphs: [
                    `Verilerinizin işlenip işlenmediğini, işlenme biçimini, amacını, amaca uygun kullanımını ve alıcılarını öğrenmek; bilgi, düzeltme veya silme talep etmek için ${operator.email} adresine başvurabilirsiniz. Uygulanabilir haklarınız; düzeltme veya silmenin alıcılara bildirilmesini istemeyi, otomatik analizle aleyhinize oluşan sonuca itirazı, hukuka aykırı işlemeden doğan zararın giderilmesini ve yetkili kuruma şikâyeti kapsar. KVKK başvuruları en geç 30 gün içinde sonuçlandırılır.`,
                    "Uygulamalarımız değiştikçe tarihli bu metni güncelleriz.",
                ],
            },
        ],
    };
}

function termsEnglish(operator: PublicOperator): LegalContent {
    return {
        kind: "terms",
        title: "Terms of use",
        intro: "Using Pyracast and understanding its solar forecasts.",
        updated: "2026-11-15",
        sections: [
            {
                id: "service",
                title: "The service",
                paragraphs: [
                    `Pyracast is operated by ${operator.name}, ${operator.address}, ${operator.country}. Contact ${operator.email} about the service or these terms.`,
                    "Pyracast brings together solar plant information, production forecasts and energy estimates. Features and data depend on plant approval, authorized access and service availability.",
                ],
            },
            {
                id: "authorized-use",
                title: "Authorized use",
                paragraphs: [
                    "Only submit or access plant information, production records and recipient addresses you are authorized to use. Keep declarations accurate and respect applicable permissions and licences.",
                    "Do not bypass access controls, misuse credentials, access other workspaces without permission, disrupt the service or use it unlawfully. We may restrict access as reasonably needed to address abuse or protect the service. Contact us about mistaken restrictions.",
                ],
            },
            {
                id: "accounts",
                title: "Accounts and workspaces",
                paragraphs: [
                    "Protect your sign-in methods and promptly report suspected unauthorized access. Workspace administrators manage membership, shared settings and reminder recipients. Only add recipients authorized to receive the messages and plant information.",
                    "Removing a member preserves shared records. Manage security and notifications in the application; contact us to request account deletion.",
                ],
            },
            {
                id: "estimates",
                title: "Understand the estimates",
                paragraphs: [
                    "Forecasts and energy estimates are model outputs, not metered production or guarantees of generation or financial performance. Weather, missing inputs, plant details, outages and calibration affect results. Check units, timestamps, completeness and assumptions.",
                    "Verify information against your operational requirements and independent sources before consequential decisions. Data availability and email delivery are not guaranteed.",
                ],
            },
            {
                id: "changes",
                title: "Changes and questions",
                paragraphs: [
                    "We may update these dated terms as the service changes. Separate agreements with the operator continue to govern the matters they cover. Our privacy policy explains how information is handled.",
                    `For questions, corrections or access concerns, write to ${operator.email}.`,
                ],
            },
        ],
    };
}

function termsTurkish(operator: PublicOperator): LegalContent {
    return {
        kind: "terms",
        title: "Kullanım koşulları",
        intro: "Pyracast kullanımı ve güneş enerjisi tahminlerinin değerlendirilmesi.",
        updated: "2026-11-15",
        sections: [
            {
                id: "service",
                title: "Hizmet",
                paragraphs: [
                    `Pyracast, ${operator.name} tarafından işletilir. Adres: ${operator.address}, ${operator.country}. Hizmet veya bu koşullarla ilgili iletişim: ${operator.email}.`,
                    "Pyracast, güneş santrali bilgilerini, üretim tahminlerini ve enerji hesaplarını bir araya getirir. Özellikler ve veriler; santral onayına, yetkili erişime ve hizmetin kullanılabilirliğine bağlıdır.",
                ],
            },
            {
                id: "authorized-use",
                title: "Yetkili kullanım",
                paragraphs: [
                    "Santral bilgilerini, üretim kayıtlarını ve alıcı adreslerini yalnızca yetkiniz varsa ekleyin veya kullanın. Beyanlarınızı doğru tutun; geçerli izinlere ve lisanslara uyun.",
                    "Erişim kontrollerini aşmayın, kimlik bilgilerini kötüye kullanmayın, izinsiz başka çalışma alanlarına erişmeyin, hizmeti aksatmayın veya hukuka aykırı kullanmayın. Kötüye kullanımı önlemek veya hizmeti korumak için makul ölçüde erişimi kısıtlayabiliriz. Hatalı kısıtlamalar için bize ulaşın.",
                ],
            },
            {
                id: "accounts",
                title: "Hesaplar ve çalışma alanları",
                paragraphs: [
                    "Giriş yöntemlerinizi koruyun; izinsiz erişim şüphenizi gecikmeden bildirin. Çalışma alanı yöneticileri üyelikleri, ortak ayarları ve hatırlatma alıcılarını yönetir. Yalnızca mesajları ve santral bilgilerini almaya yetkili alıcıları ekleyin.",
                    "Bir üyenin çıkarılması ortak kayıtları silmez. Güvenlik ve bildirim ayarlarını uygulamadan yönetin; hesap silme talepleri için bize ulaşın.",
                ],
            },
            {
                id: "estimates",
                title: "Tahminleri değerlendirin",
                paragraphs: [
                    "Tahminler ve enerji hesapları model çıktılarıdır; ölçülmüş üretim veya üretim ve finansal performans garantisi değildir. Hava koşulları, eksik girdiler, santral bilgileri, kesintiler ve kalibrasyon sonuçları etkiler. Birimleri, zaman bilgilerini, veri bütünlüğünü ve varsayımları kontrol edin.",
                    "Önemli sonuçlar doğurabilecek kararlardan önce bilgileri işletim gereksinimlerinize ve bağımsız kaynaklara göre doğrulayın. Verilerin sürekli erişilebilirliği ve e-posta teslimi garanti edilmez.",
                ],
            },
            {
                id: "changes",
                title: "Değişiklikler ve sorular",
                paragraphs: [
                    "Hizmet değiştikçe tarihli bu koşulları güncelleyebiliriz. İşletmeciyle ayrı sözleşmeleriniz, kapsadıkları konularda geçerliliğini korur. Gizlilik politikamız bilgilerin nasıl işlendiğini açıklar.",
                    `Sorular, düzeltmeler veya erişim sorunları için ${operator.email} adresine yazın.`,
                ],
            },
        ],
    };
}

export function legalCopy(language: Language, kind: LegalKind): LegalContent {
    const operator: PublicOperator = {
        ...operatorDetails,
        address: operatorDetails.address[language],
    };
    if (kind === "privacy") {
        return language === "tr"
            ? privacyTurkish(operator)
            : privacyEnglish(operator);
    }
    return language === "tr" ? termsTurkish(operator) : termsEnglish(operator);
}
