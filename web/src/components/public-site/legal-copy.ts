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
        title: "Privacy & personal data",
        intro: "How Pyracast handles your account and solar forecasting data.",
        updated: "2026-11-15",
        sections: [
            {
                id: "operator",
                title: "Who operates Pyracast",
                paragraphs: [
                    `${operator.name} operates Pyracast and is responsible for this processing. Address: ${operator.address}, ${operator.country}. Contact: ${operator.email}.`,
                ],
            },
            {
                id: "information",
                title: "Information we handle",
                paragraphs: [
                    "Sources include you, workspace administrators, sign-in providers and authorized forecast or production services.",
                ],
                bullets: [
                    "Email, password hash if applicable, provider identity, language, membership and security settings.",
                    "Workspace and plant names, coordinates, declarations, production, incidents, forecasts, calibration and change histories.",
                    "Invitation and reminder addresses, schedules, messages and delivery records.",
                    "Sessions, authentication records, IP addresses and operational logs.",
                ],
            },
            {
                id: "purposes",
                title: "Why we use it",
                paragraphs: [
                    "We manage accounts and workspace access, provide forecasts, send service messages and requested reminders, support users and investigate failures or abuse.",
                    "Service processing fulfils your request and, where applicable, our contract. Security processing serves our legitimate interest in protecting the service, subject to your rights. We also meet applicable legal obligations. You control optional connections and reminders in the application.",
                ],
            },
            {
                id: "sign-in",
                title: "Google and Microsoft sign-in",
                paragraphs: [
                    "Google requests openid and email; Microsoft requests User.Read. We authenticate or link accounts using provider identity and email. Responses may include other basic profile fields; we retain the provider identifier and email, without sign-in access or refresh tokens.",
                    "Sign-in gives no mailbox access. A separate Google Workspace sender uses Gmail send permission for service email. Disconnect providers in Account → Security when another working sign-in method exists. Disconnecting does not delete your account or workspace records.",
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
                    "Workspace members see authorized shared records; administrators manage membership and settings. Operators access records for administration and support. Reminder recipients receive configured messages.",
                    "Google Cloud hosts the application and database in Belgium (europe-west1), with backups and logging. Cloudflare handles traffic globally; Google Workspace delivers email; Google or Microsoft handles chosen sign-in. Authorized Solcast, EPİAŞ and optional Open-Meteo integrations receive relevant plant identifiers, coordinates or request details. These services may process data internationally; Belgium hosting does not confine all processing there. Legally required disclosures may also occur.",
                ],
            },
            {
                id: "storage",
                title: "Cookies and browser storage",
                paragraphs: [
                    "Session and CSRF cookies support security; a language cookie lasts up to one year. Session storage supports invitations and sign-in continuation. Sessions expire at browser close, 30 minutes of inactivity or 12 hours. The application includes no advertising or behavioral analytics trackers. Clearing browser data may end sessions or reset preferences.",
                ],
            },
            {
                id: "retention",
                title: "Retention and deletion",
                paragraphs: [
                    "We retain records as needed for service operation, shared workspaces, security and applicable obligations. Deleted plants and locations have a 30-day restore period before scheduled cleanup. Backups expire after seven days by configuration; cleanup is asynchronous, without immediate removal from every copy.",
                    `Request deletion or a retention review at ${operator.email}. We may verify identity and workspace ownership. Removing membership or connections preserves other members' shared records. We explain any required retention.`,
                ],
            },
            {
                id: "rights",
                title: "Your questions and rights",
                paragraphs: [
                    `Contact ${operator.email} about processing, purposes, recipients, access, correction or deletion. Applicable rights may include notifying recipients of corrections/deletions, objecting to adverse automated decisions, compensation for unlawful processing and complaints to authorities. Turkish KVKK requests must be concluded within 30 days.`,
                    "We update this dated notice when practices change.",
                ],
            },
        ],
    };
}

function privacyTurkish(operator: PublicOperator): LegalContent {
    return {
        kind: "privacy",
        title: "Gizlilik ve kişisel veriler",
        intro: "Pyracast'in hesap bilgilerinizi ve güneş enerjisi tahminlerini yönetirken kullandığınız verileri nasıl işlediği.",
        updated: "2026-11-15",
        sections: [
            {
                id: "operator",
                title: "Pyracast'i kim işletiyor?",
                paragraphs: [
                    `${operator.name}, Pyracast'i işletir ve burada açıklanan kişisel veri işleme faaliyetlerinden sorumludur. Adres: ${operator.address}, ${operator.country}. İletişim: ${operator.email}.`,
                ],
            },
            {
                id: "information",
                title: "İşlediğimiz bilgiler",
                paragraphs: [
                    "Bilgileri sizden, çalışma alanı yöneticilerinizden, bağladığınız giriş sağlayıcılarından ve yetkili tahmin veya üretim kaynaklarından alırız.",
                ],
                bullets: [
                    "Hesap e-postası, parola kullanıyorsanız parola özeti, bağlı sağlayıcı kimliği, dil tercihi, üyelik ve güvenlik ayarları.",
                    "Çalışma alanı ve tesis adları, koordinatlar, teknik beyanlar, üretim, olaylar, tahminler, kalibrasyon ve değişikliklerle ilişkili kişiler ile zaman bilgileri.",
                    "Davet ve hatırlatma alıcı adresleri, programlar, mesaj içerikleri ve gönderim kayıtları.",
                    "Hizmeti korumak ve sürdürmek için gerekli oturum ve kimlik doğrulama kayıtları, IP adresleri ve işletim günlükleri.",
                ],
            },
            {
                id: "purposes",
                title: "Neden kullanıyoruz?",
                paragraphs: [
                    "Bu bilgileri hesap oluşturmak, çalışma alanı erişimini yönetmek, tahminleri hazırlayıp göstermek, talep edilen hizmet mesajlarını ve hatırlatmaları göndermek, destek sağlamak ve hataları veya kötüye kullanımı incelemek için kullanırız.",
                    "Hizmetin sunulması için gerekli işlemler, hizmet talebinizin karşılanmasına ve uygulanabildiği ölçüde sizinle kurulan sözleşmenin ifasına dayanır. Güvenlik ve süreklilik işlemleri, haklarınız gözetilerek hizmeti korumaya yönelik meşru menfaatimiz kapsamında yürütülür. Hukuki yükümlülüklerin gerektirdiği bilgileri de işleriz. İsteğe bağlı sağlayıcı bağlantıları ve hatırlatma ayarları uygulamadan yönetilir.",
                ],
            },
            {
                id: "sign-in",
                title: "Google ve Microsoft ile giriş",
                paragraphs: [
                    "Google ile giriş openid ve email; Microsoft ile giriş User.Read izinlerini ister. Dönen kimlik ve e-posta bilgilerini giriş yapmak veya hesabınızı bağlamak için kullanırız; sağlayıcı yanıtı başka temel profil alanları da içerebilir. Sağlayıcı kimliğini ve e-postayı saklarız; giriş için kullanılan erişim veya yenileme belirteçlerini saklamayız.",
                    "Giriş yapmak Pyracast'e posta kutunuza erişim vermez. Ayrı Google Workspace gönderici hesabımız, hizmet e-postalarını iletmek için Gmail gönderim iznini kullanır; gelen kutunuza erişim istemez. Başka bir çalışan giriş yöntemi mevcutsa Hesap → Güvenlik bölümünden sağlayıcı bağlantısını kaldırabilirsiniz. Bağlantıyı kaldırmak Pyracast hesabınızı veya çalışma alanı kayıtlarını silmez.",
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
                    "Yetkili üyeler paylaşılan çalışma alanı kayıtlarını görebilir; yöneticiler üyelik ve ayarları yönetir. Platform işletmecileri hizmet yönetimi ve destek için kayıtlara erişir. Hatırlatma alıcıları, ayarlanan tahmin mesajlarını alır.",
                    "Google Cloud uygulama ve veritabanını Belçika'da (europe-west1) barındırır; yedekleme ve işletim günlükleri için de kullanılır. Cloudflare web trafiğini küresel ağı üzerinden işler. Google Workspace hizmet e-postalarını gönderir. Seçtiğinizde Google veya Microsoft giriş işlemini yürütür. Yetkili Solcast, EPİAŞ ve isteğe bağlı Open-Meteo bağlantılarına, işlevleri için ilgili tesis kimlikleri, koordinatlar veya istek bilgileri iletilir. Bu hizmetler bilgileri uluslararası olarak işleyebilir; Belçika'da barındırma tüm işlemlerin orada kaldığı anlamına gelmez. Kanunen gerekli olduğunda da bilgi paylaşılabilir.",
                ],
            },
            {
                id: "storage",
                title: "Çerezler ve tarayıcı depolaması",
                paragraphs: [
                    "Pyracast, giriş ve istek güvenliği için oturum ve CSRF çerezlerini, bir yıla kadar saklanan dil çerezini ve davet ile giriş akışını sürdürmek için oturum depolamasını kullanır. Oturumlar tarayıcı kapatıldığında, 30 dakika işlem yapılmadığında veya 12 saat sonunda biter. Uygulamada reklam veya davranış analizi izleyicileri bulunmaz. Tarayıcınızdan saklanan verileri silebilirsiniz; bu işlem oturumunuzu kapatabilir veya tercihlerinizi sıfırlayabilir.",
                ],
            },
            {
                id: "retention",
                title: "Saklama ve silme",
                paragraphs: [
                    "Hesap ve çalışma alanı kayıtları hizmeti yürütmek, paylaşılan kayıtları korumak, güvenliği sağlamak ve uygulanabilir yükümlülükleri yerine getirmek için gerektiği sürece saklanır. Silinen tesis ve konumların 30 günlük geri yükleme süresi vardır; ardından zamanlanmış temizleme yapılır. Veritabanı yedekleri yedi gün sonra temizlenecek şekilde ayarlanır; arka plan temizliği eşzamansızdır. Bu süreler her kopyadan anında silinme taahhüdü değildir.",
                    `Hesap silme veya saklama incelemesi talebinizi ${operator.email} adresine iletin. İşlem öncesinde kimliğinizi doğrulamamız ve çalışma alanı sahipliğini netleştirmemiz gerekebilir. Üyeliği veya sağlayıcı bağlantısını kaldırmak, diğer üyelerin kullandığı paylaşılan kayıtları silmez. Saklanması gereken bilgiler varsa nedenlerini açıklarız.`,
                ],
            },
            {
                id: "rights",
                title: "Sorularınız ve haklarınız",
                paragraphs: [
                    `Verilerinizin işlenip işlenmediğini, amacını, amacına uygun kullanımını ve kimlere aktarıldığını öğrenmek; bilgi, düzeltme veya silme talep etmek için ${operator.email} adresine başvurabilirsiniz. Uygulanabilir hukuk kapsamında düzeltme ve silmenin alıcılara bildirilmesini isteyebilir, yalnızca otomatik analizle aleyhinize oluşan sonuca itiraz edebilir, hukuka aykırı işleme nedeniyle zararınızın giderilmesini talep edebilir ve yetkili kuruma şikâyette bulunabilirsiniz. Başvuruları uygulanabilir hukuki gerekliliklere göre yanıtlarız; KVKK kapsamındaki başvurular en geç 30 gün içinde sonuçlandırılır.`,
                    "Uygulamalarımız değiştikçe bu metni güncelleriz. Yukarıdaki tarih güncel sürümü gösterir.",
                ],
            },
        ],
    };
}

function termsEnglish(operator: PublicOperator): LegalContent {
    return {
        kind: "terms",
        title: "Terms of use",
        intro: "The working rules for using Pyracast and interpreting its solar forecasts.",
        updated: "2026-11-15",
        sections: [
            {
                id: "service",
                title: "The service",
                paragraphs: [
                    `Pyracast is operated by ${operator.name}, ${operator.address}, ${operator.country}. Contact ${operator.email} about the service or these terms.`,
                    "Pyracast organizes authorized solar plant information, irradiance forecasts and estimated energy production. Available features and data depend on plant approval, provider permissions, saved records and service availability.",
                ],
            },
            {
                id: "authorized-use",
                title: "Use data you are authorized to use",
                paragraphs: [
                    "Submit and access plants, production records and recipient addresses only when you have the necessary authority. Keep declarations accurate and respect the applicable provider permissions and licences. Submitting a plant does not create a provider account or guarantee approval, coverage or access to a particular dataset.",
                    "Do not attempt to bypass access controls, access another workspace without permission, misuse credentials, evade provider limits, disrupt the service or use it for unlawful activity. We may restrict or suspend access where reasonably necessary to address abuse or protect the service; contact us if you believe a restriction is mistaken.",
                ],
            },
            {
                id: "accounts",
                title: "Accounts and workspaces",
                paragraphs: [
                    "Protect your sign-in methods and tell us promptly if you suspect unauthorized access. Workspace administrators manage invitations, membership, shared settings and reminder recipients. Members can access the shared information permitted by their role. Only add recipients who are authorized to receive the messages and plant information.",
                    "A member's removal does not delete the workspace or its shared records. Provider connections, account security and notification settings can be managed in the application; contact us for account deletion requests.",
                ],
            },
            {
                id: "estimates",
                title: "Understand the estimates",
                paragraphs: [
                    "Forecasts, irradiance-derived energy and retrospective estimates are model outputs. They are not metered production, a guarantee of future generation or a promise of financial performance. Weather, missing source data, technical declarations, outages and calibration can affect results. Check the displayed source, units, timestamps, completeness and assumptions.",
                    "Verify information against your operational requirements and suitable independent sources before making dispatch, commercial or other consequential decisions. Provider or email acceptance does not guarantee an uninterrupted feed or delivery to an inbox.",
                ],
            },
            {
                id: "changes",
                title: "Changes and questions",
                paragraphs: [
                    "We may update these terms as the service changes. This page shows the current version date. Any separate agreement you have with the operator continues to govern the matters it covers. Our privacy notice explains how information is handled.",
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
        intro: "Pyracast'i kullanırken ve güneş enerjisi tahminlerini değerlendirirken geçerli temel kurallar.",
        updated: "2026-11-15",
        sections: [
            {
                id: "service",
                title: "Hizmet",
                paragraphs: [
                    `Pyracast, ${operator.name} tarafından işletilir. Adres: ${operator.address}, ${operator.country}. Hizmet veya bu koşullarla ilgili iletişim: ${operator.email}.`,
                    "Pyracast, yetkili güneş enerjisi tesisi bilgilerini, ışınım tahminlerini ve enerji üretim tahminlerini bir araya getirir. Kullanılabilir özellik ve veriler tesis onayına, sağlayıcı izinlerine, kayıtlı verilere ve hizmetin erişilebilirliğine bağlıdır.",
                ],
            },
            {
                id: "authorized-use",
                title: "Yetkiniz olan verileri kullanın",
                paragraphs: [
                    "Tesisleri, üretim kayıtlarını ve alıcı adreslerini yalnızca gerekli yetkiniz varsa ekleyin ve kullanın. Beyanlarınızı doğru tutun; sağlayıcı izinlerine ve lisanslarına uyun. Tesis göndermek sağlayıcı hesabı oluşturmaz; onayı, veri kapsamını veya belirli bir veri kümesine erişimi garanti etmez.",
                    "Erişim kontrollerini aşmaya, izinsiz başka çalışma alanlarına erişmeye, kimlik bilgilerini kötüye kullanmaya, sağlayıcı sınırlarını dolanmaya veya hizmeti aksatmaya çalışmayın; hizmeti hukuka aykırı amaçla kullanmayın. Kötüye kullanımı önlemek veya hizmeti korumak için makul ölçüde gerekli olduğunda erişimi kısıtlayabilir ya da askıya alabiliriz. Kısıtlamanın hatalı olduğunu düşünüyorsanız bizimle iletişime geçin.",
                ],
            },
            {
                id: "accounts",
                title: "Hesaplar ve çalışma alanları",
                paragraphs: [
                    "Giriş yöntemlerinizi koruyun; izinsiz erişimden şüphelenirseniz bize gecikmeden bildirin. Çalışma alanı yöneticileri davetleri, üyelikleri, ortak ayarları ve hatırlatma alıcılarını yönetir. Üyeler rollerinin izin verdiği ortak bilgilere erişebilir. Yalnızca mesajları ve tesis bilgilerini almaya yetkili alıcıları ekleyin.",
                    "Bir üyenin çıkarılması çalışma alanını veya ortak kayıtlarını silmez. Sağlayıcı bağlantıları, hesap güvenliği ve bildirim ayarları uygulamadan yönetilebilir; hesap silme talepleri için bize başvurun.",
                ],
            },
            {
                id: "estimates",
                title: "Tahminleri değerlendirin",
                paragraphs: [
                    "Tahminler, ışınımdan hesaplanan enerji ve geçmişe dönük kestirimler model çıktılarıdır. Ölçülmüş üretim, gelecekteki üretim garantisi veya finansal performans taahhüdü değildir. Hava koşulları, eksik kaynak verileri, teknik beyanlar, kesintiler ve kalibrasyon sonuçları etkileyebilir. Gösterilen kaynağı, birimleri, zaman bilgilerini, veri bütünlüğünü ve varsayımları kontrol edin.",
                    "İşletim, ticaret veya önemli sonuçlar doğurabilecek diğer kararlar öncesinde bilgileri kendi gereksinimlerinize ve uygun bağımsız kaynaklara göre doğrulayın. Sağlayıcının veya e-posta hizmetinin kabulü, kesintisiz veri akışını ya da gelen kutusuna teslimi garanti etmez.",
                ],
            },
            {
                id: "changes",
                title: "Değişiklikler ve sorular",
                paragraphs: [
                    "Hizmet değiştikçe bu koşulları güncelleyebiliriz. Bu sayfa güncel sürüm tarihini gösterir. İşletmeciyle ayrı bir sözleşmeniz varsa kapsadığı konularda o sözleşme geçerliliğini sürdürür. Gizlilik metnimiz bilgilerin nasıl işlendiğini açıklar.",
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
