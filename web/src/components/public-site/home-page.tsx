import { homeCopy, type PublicLanguage } from "./home-copy";
import { SiteShell } from "./site-shell";
import styles from "./public-site.module.css";

export function HomePage({ language }: { language: PublicLanguage }) {
    const copy = homeCopy[language];
    return (
        <SiteShell language={language}>
            <main id="main-content">
                <section className={styles.hero} aria-labelledby="home-title">
                    <div className={styles.heroInner}>
                        <div className={styles.heroCopy}>
                            <p className={styles.eyebrow}>
                                <span
                                    className={styles.sunDot}
                                    aria-hidden="true"
                                />
                                {copy.eyebrow}
                            </p>
                            <h1 id="home-title">{copy.title}</h1>
                            <p className={styles.heroIntro}>{copy.intro}</p>
                            <div className={styles.heroActions}>
                                <a
                                    className={styles.primaryButton}
                                    href={`/app/?signup=1&language=${language}`}
                                >
                                    {copy.signup}
                                    <span aria-hidden="true"> ↗</span>
                                </a>
                                <a
                                    className={styles.textLink}
                                    href="#how-it-works"
                                >
                                    {copy.secondary}
                                    <span aria-hidden="true"> ↓</span>
                                </a>
                            </div>
                            <p className={styles.approval}>{copy.approval}</p>
                        </div>
                        <figure
                            className={styles.preview}
                            aria-labelledby="preview-caption"
                        >
                            <div className={styles.previewTop}>
                                <span
                                    className={styles.previewDot}
                                    aria-hidden="true"
                                />
                                {copy.illustration}
                                <span
                                    className={styles.previewMark}
                                    aria-hidden="true"
                                >
                                    ☀
                                </span>
                            </div>
                            <div className={styles.previewContent}>
                                <p className={styles.previewEyebrow}>
                                    {copy.previewLabel}
                                </p>
                                <h2>{copy.chartTitle}</h2>
                                <p className={styles.previewSubtitle}>
                                    {copy.chartSubtitle}
                                </p>
                                <div className={styles.chart}>
                                    <span className={styles.chartAxis}>
                                        {copy.chartAxis}
                                    </span>
                                    <svg
                                        viewBox="0 0 480 230"
                                        role="img"
                                        aria-label={copy.chartSubtitle}
                                    >
                                        <defs>
                                            <linearGradient
                                                id="public-forecast-fill"
                                                x1="0"
                                                y1="0"
                                                x2="0"
                                                y2="1"
                                            >
                                                <stop
                                                    offset="0%"
                                                    stopColor="#176b56"
                                                    stopOpacity="0.18"
                                                />
                                                <stop
                                                    offset="100%"
                                                    stopColor="#176b56"
                                                    stopOpacity="0.01"
                                                />
                                            </linearGradient>
                                        </defs>
                                        <g stroke="#dfe6df" strokeWidth="1">
                                            <path d="M10 30H470M10 85H470M10 140H470M10 195H470" />
                                        </g>
                                        <path
                                            d="M10 195C60 195 75 188 100 160S140 85 180 60S225 30 255 53S290 105 320 115S370 155 395 177S440 194 470 195V195H10Z"
                                            fill="url(#public-forecast-fill)"
                                        />
                                        <path
                                            d="M10 195C60 195 75 188 100 160S140 85 180 60S225 30 255 53S290 105 320 115S370 155 395 177S440 194 470 195"
                                            fill="none"
                                            stroke="#176b56"
                                            strokeWidth="3"
                                            strokeLinecap="round"
                                        />
                                        <path
                                            d="M10 195C60 195 78 184 102 166S142 94 181 73S221 50 251 67"
                                            fill="none"
                                            stroke="#a56a12"
                                            strokeWidth="3"
                                            strokeDasharray="5 6"
                                            strokeLinecap="round"
                                        />
                                        <circle
                                            cx="251"
                                            cy="67"
                                            r="5"
                                            fill="#a56a12"
                                            stroke="#fff"
                                            strokeWidth="3"
                                        />
                                        <g
                                            fill="#5c6d65"
                                            fontSize="12"
                                            fontFamily="inherit"
                                        >
                                            <text x="10" y="222">
                                                06:00
                                            </text>
                                            <text x="120" y="222">
                                                09:00
                                            </text>
                                            <text x="230" y="222">
                                                12:00
                                            </text>
                                            <text x="340" y="222">
                                                15:00
                                            </text>
                                            <text x="438" y="222">
                                                18:00
                                            </text>
                                        </g>
                                    </svg>
                                </div>
                                <div className={styles.legend}>
                                    <span>
                                        <i className={styles.forecastLine} />
                                        {copy.forecast}
                                    </span>
                                    <span>
                                        <i className={styles.actualLine} />
                                        {copy.actual}
                                    </span>
                                </div>
                            </div>
                            <figcaption
                                id="preview-caption"
                                className={styles.previewCaption}
                            >
                                {copy.chartNote}
                            </figcaption>
                        </figure>
                    </div>
                </section>
                <section
                    id="product"
                    className={styles.benefits}
                    aria-labelledby="benefits-title"
                >
                    <div className={styles.sectionHeading}>
                        <p className={styles.eyebrow}>{copy.benefitsEyebrow}</p>
                        <h2 id="benefits-title">{copy.benefitsTitle}</h2>
                    </div>
                    <div className={styles.benefitGrid}>
                        {copy.benefits.map((benefit, index) => (
                            <article
                                className={styles.benefit}
                                key={benefit.title}
                            >
                                <span
                                    className={styles.benefitIcon}
                                    aria-hidden="true"
                                >
                                    {index === 0 ? (
                                        <svg viewBox="0 0 24 24" fill="none">
                                            <path
                                                d="M4 18V6m0 12h16M7 14l4-6 4 3 5-6"
                                                stroke="currentColor"
                                                strokeWidth="1.7"
                                                strokeLinecap="round"
                                                strokeLinejoin="round"
                                            />
                                        </svg>
                                    ) : index === 1 ? (
                                        <svg viewBox="0 0 24 24" fill="none">
                                            <path
                                                d="M5 7h14M5 12h9M5 17h6m5-1 2 2 4-5"
                                                stroke="currentColor"
                                                strokeWidth="1.7"
                                                strokeLinecap="round"
                                                strokeLinejoin="round"
                                            />
                                        </svg>
                                    ) : (
                                        <svg viewBox="0 0 24 24" fill="none">
                                            <circle
                                                cx="9"
                                                cy="8"
                                                r="3"
                                                stroke="currentColor"
                                                strokeWidth="1.7"
                                            />
                                            <path
                                                d="M3 20v-2a6 6 0 0 1 12 0v2m1-15a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v2"
                                                stroke="currentColor"
                                                strokeWidth="1.7"
                                                strokeLinecap="round"
                                            />
                                        </svg>
                                    )}
                                </span>
                                <h3>{benefit.title}</h3>
                                <p>{benefit.text}</p>
                            </article>
                        ))}
                    </div>
                </section>
                <section
                    id="how-it-works"
                    className={styles.stepsSection}
                    aria-labelledby="steps-title"
                >
                    <div className={styles.stepsInner}>
                        <div className={styles.stepsHeading}>
                            <p className={styles.eyebrow}>
                                {copy.stepsEyebrow}
                            </p>
                            <h2 id="steps-title">{copy.stepsTitle}</h2>
                        </div>
                        <ol className={styles.steps}>
                            {copy.steps.map((step, index) => (
                                <li key={step.title}>
                                    <span
                                        className={styles.stepNumber}
                                        aria-hidden="true"
                                    >
                                        0{index + 1}
                                    </span>
                                    <div>
                                        <h3>{step.title}</h3>
                                        <p>{step.text}</p>
                                    </div>
                                </li>
                            ))}
                        </ol>
                    </div>
                </section>
                <section
                    id="questions"
                    className={styles.faq}
                    aria-labelledby="faq-title"
                >
                    <h2 id="faq-title">{copy.faqTitle}</h2>
                    <div className={styles.questions}>
                        {copy.faq.map((item) => (
                            <details key={item.question}>
                                <summary>
                                    {item.question}
                                    <span aria-hidden="true">+</span>
                                </summary>
                                <p>{item.answer}</p>
                            </details>
                        ))}
                    </div>
                </section>
                <section className={styles.cta} aria-labelledby="cta-title">
                    <div>
                        <p className={styles.eyebrow}>Pyracast</p>
                        <h2 id="cta-title">{copy.ctaTitle}</h2>
                        <p>{copy.ctaText}</p>
                    </div>
                    <a
                        className={styles.lightButton}
                        href={`/app/?signup=1&language=${language}`}
                    >
                        {copy.signup}
                        <span aria-hidden="true"> ↗</span>
                    </a>
                </section>
            </main>
        </SiteShell>
    );
}
