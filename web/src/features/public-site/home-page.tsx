import { homeCopy, type PublicLanguage } from "./home-copy";
import { SiteShell } from "./site-shell";
import { routes } from "@/lib/routes";
import styles from "./public-site.module.css";

export function HomePage({ language }: { language: PublicLanguage }) {
    const copy = homeCopy[language];
    const signupHref = routes.signUp(language);
    return (
        <SiteShell language={language}>
            <main id="main-content" tabIndex={-1}>
                <section className={styles.hero} aria-labelledby="home-title">
                    <div className={styles.heroInner}>
                        <div className={styles.heroCopy}>
                            <p className={styles.eyebrow}>{copy.eyebrow}</p>
                            <h1 id="home-title">{copy.title}</h1>
                            <p className={styles.heroIntro}>{copy.intro}</p>
                            <div className={styles.heroActions}>
                                <a
                                    className={styles.primaryButton}
                                    href={signupHref}
                                >
                                    {copy.signup}
                                    <span aria-hidden="true">↗</span>
                                </a>
                                <a className={styles.textLink} href="#product">
                                    {copy.secondary}
                                    <span aria-hidden="true">↓</span>
                                </a>
                            </div>
                        </div>
                        <figure className={styles.forecastPanel}>
                            <div className={styles.forecastHeading}>
                                <h2>{copy.chartTitle}</h2>
                                <svg
                                    width="28"
                                    height="28"
                                    viewBox="0 0 28 28"
                                    fill="none"
                                    aria-hidden="true"
                                >
                                    <path
                                        d="M3 21h22M6 17a8 8 0 0 1 16 0M14 3v3M4 7l2 2m18-2-2 2"
                                        stroke="currentColor"
                                        strokeWidth="1.5"
                                        strokeLinecap="round"
                                    />
                                </svg>
                            </div>
                            <div className={styles.chart}>
                                <span className={styles.chartAxis}>
                                    {copy.chartAxis}
                                </span>
                                <svg
                                    viewBox="0 0 480 240"
                                    role="img"
                                    aria-labelledby="forecast-chart-title"
                                >
                                    <title id="forecast-chart-title">
                                        {copy.chartDescription}
                                    </title>
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
                                                stopColor="#28664e"
                                                stopOpacity="0.17"
                                            />
                                            <stop
                                                offset="100%"
                                                stopColor="#28664e"
                                                stopOpacity="0"
                                            />
                                        </linearGradient>
                                    </defs>
                                    <g
                                        stroke="#dedfd3"
                                        strokeWidth="1"
                                        strokeDasharray="3 5"
                                    >
                                        <path d="M4 32H476M4 88H476M4 144H476" />
                                    </g>
                                    <path
                                        d="M4 210H476"
                                        stroke="#c4ccbd"
                                        strokeWidth="1"
                                    />
                                    <path
                                        d="M4 210C52 210 66 201 94 173S137 99 177 66S222 35 252 57S289 105 321 127S369 171 399 192S442 210 476 210V210H4Z"
                                        fill="url(#public-forecast-fill)"
                                    />
                                    <path
                                        d="M4 210C52 210 66 201 94 173S137 99 177 66S222 35 252 57S289 105 321 127S369 171 399 192S442 210 476 210"
                                        fill="none"
                                        stroke="#28664e"
                                        strokeWidth="3"
                                        strokeLinecap="round"
                                    />
                                    <path
                                        d="M4 210C52 210 70 201 96 180S138 111 177 81S222 56 252 75"
                                        fill="none"
                                        stroke="#a17537"
                                        strokeWidth="3"
                                        strokeDasharray="5 7"
                                        strokeLinecap="round"
                                    />
                                    <circle
                                        cx="252"
                                        cy="75"
                                        r="5"
                                        fill="#a17537"
                                        stroke="#fbfbf5"
                                        strokeWidth="3"
                                    />
                                </svg>
                                <div
                                    className={styles.chartTimes}
                                    aria-hidden="true"
                                >
                                    <span>06:00</span>
                                    <span>12:00</span>
                                    <span>18:00</span>
                                </div>
                            </div>
                            <div className={styles.legend}>
                                <span>
                                    <i
                                        className={styles.forecastLine}
                                        aria-hidden="true"
                                    />
                                    {copy.forecast}
                                </span>
                                <span>
                                    <i
                                        className={styles.actualLine}
                                        aria-hidden="true"
                                    />
                                    {copy.actual}
                                </span>
                            </div>
                            <figcaption className={styles.forecastCaption}>
                                {copy.chartNote}
                            </figcaption>
                        </figure>
                    </div>
                </section>
                <section
                    id="product"
                    className={styles.product}
                    aria-labelledby="product-title"
                >
                    <div className={styles.productHeading}>
                        <p className={styles.eyebrow}>{copy.productEyebrow}</p>
                        <h2 id="product-title">{copy.productTitle}</h2>
                    </div>
                    <div className={styles.featureList}>
                        {copy.features.map((feature, index) => (
                            <article
                                className={styles.feature}
                                key={feature.title}
                            >
                                <span
                                    className={styles.featureNumber}
                                    aria-hidden="true"
                                >
                                    0{index + 1}
                                </span>
                                <div>
                                    <h3>{feature.title}</h3>
                                    <p>{feature.text}</p>
                                </div>
                            </article>
                        ))}
                    </div>
                </section>
                <section
                    id="getting-started"
                    className={styles.start}
                    aria-labelledby="start-title"
                >
                    <div className={styles.startInner}>
                        <div className={styles.startHeading}>
                            <p className={styles.eyebrow}>
                                {copy.startEyebrow}
                            </p>
                            <h2 id="start-title">{copy.startTitle}</h2>
                        </div>
                        <div className={styles.startAction}>
                            <p>{copy.startText}</p>
                            <a
                                className={styles.primaryButton}
                                href={signupHref}
                            >
                                {copy.signup}
                                <span aria-hidden="true">↗</span>
                            </a>
                        </div>
                    </div>
                </section>
            </main>
        </SiteShell>
    );
}
