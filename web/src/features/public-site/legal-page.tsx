import { homeCopy, type PublicLanguage } from "./home-copy";
import { formatDay } from "@/lib/format/time";
import { publicPath } from "@/lib/public-metadata";
import { SiteShell } from "./site-shell";
import styles from "./public-site.module.css";
import type { LegalContent } from "./legal-copy";

export function LegalPage({
    language,
    kind,
    title,
    intro,
    updated,
    sections,
}: LegalContent & { language: PublicLanguage }) {
    const copy = homeCopy[language];
    return (
        <SiteShell language={language} page={kind}>
            <main id="main-content" className={styles.legalMain} tabIndex={-1}>
                <a
                    className={styles.backLink}
                    href={publicPath(language, "home")}
                >
                    <span aria-hidden="true">← </span>
                    {copy.back}
                </a>
                <header className={styles.legalHeader}>
                    <p className={styles.eyebrow}>Pyracast</p>
                    <h1>{title}</h1>
                    <p className={styles.legalIntro}>{intro}</p>
                    <p className={styles.updated}>
                        {copy.updated}:{" "}
                        <time dateTime={updated}>
                            {formatDay(language, updated)}
                        </time>
                    </p>
                </header>
                <div className={styles.legalLayout}>
                    <nav className={styles.contents} aria-label={copy.contents}>
                        <h2>{copy.contents}</h2>
                        <ol>
                            {sections.map((section) => (
                                <li key={section.id}>
                                    <a href={`#${section.id}`}>
                                        {section.title}
                                    </a>
                                </li>
                            ))}
                        </ol>
                    </nav>
                    <div className={styles.legalBody}>
                        {sections.map((section) => (
                            <section
                                key={section.id}
                                id={section.id}
                                className={styles.legalSection}
                            >
                                <h2>{section.title}</h2>
                                {section.paragraphs.map((paragraph) => (
                                    <p key={paragraph}>{paragraph}</p>
                                ))}
                                {section.bullets && (
                                    <ul>
                                        {section.bullets.map((bullet) => (
                                            <li key={bullet}>{bullet}</li>
                                        ))}
                                    </ul>
                                )}
                                {section.links?.map((link) => (
                                    <p
                                        className={styles.legalLink}
                                        key={link.href}
                                    >
                                        <a href={link.href}>{link.label}</a>
                                    </p>
                                ))}
                            </section>
                        ))}
                        <p className={styles.legalContact}>
                            {copy.contact}:{" "}
                            <a href="mailto:info@pyracast.app">
                                info@pyracast.app
                            </a>
                        </p>
                    </div>
                </div>
            </main>
        </SiteShell>
    );
}
