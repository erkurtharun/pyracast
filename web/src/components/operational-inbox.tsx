"use client";
import { useEffect, useRef, useState } from "react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import type { Tenant } from "@/lib/api";
import { api, buildUrl } from "@/lib/api/client";
import { cadence, useApiQuery, useRefresh } from "@/lib/api/query";
import type { components } from "@/lib/schema";
import { ActionForm, Notice, RefreshFailed, Tag } from "./ui";

type ChangeRequest = components["schemas"]["ChangeRequestOutput"];
type Summary = components["schemas"]["ChangeRequestSummaryOutput"];
type Scope = "pending" | "history" | "mine";
type Reading = components["schemas"]["ProductionValueOutput"];
type Decision = "approve" | "reject" | "withdraw" | "seen";

export function OwnPendingRequests({
    tenantId,
    plantId,
}: {
    tenantId: number;
    plantId: number;
}) {
    const t = useTranslations("ChangeRequests");
    const items = useApiQuery(
        "/api/tenants/{tenant_id}/change-requests/",
        {
            path: { tenant_id: tenantId },
            query: { scope: "pending", plant_id: plantId },
        },
        { poll: cadence.background },
    );
    const count = items.data?.count ?? 0;
    if (!count) return null;
    return <Notice>{t("pendingContext", { count })}</Notice>;
}

export function OperationalInbox({ tenant }: { tenant: Tenant }) {
    const t = useTranslations("ChangeRequests");
    const refresh = useRefresh();
    const [scope, setScope] = useState<Scope>("pending");
    const [update, setUpdate] = useState<{
        revision: number;
        outcome: Decision | null;
        reset: boolean;
    }>({ revision: 0, outcome: null, reset: false });
    const feedback = useRef<HTMLDivElement>(null);
    const [page, setPage] = useState(1);
    const admin = tenant.role === "admin";
    const effectiveScope = admin ? scope : "mine";
    const changed = (outcome: Decision | null = null) => {
        const reset =
            outcome !== null &&
            outcome !== "seen" &&
            effectiveScope === "pending";
        if (reset) setPage(1);
        setUpdate((value) => ({
            revision: value.revision + 1,
            outcome,
            reset,
        }));
    };
    useEffect(() => {
        if (update.revision > 0)
            void refresh.workspace(
                tenant.id,
                update.reset
                    ? {
                          path: "/api/tenants/{tenant_id}/change-requests/",
                          options: {
                              path: { tenant_id: tenant.id },
                              query: { scope: "pending", page: 1 },
                          },
                      }
                    : undefined,
            );
        if (update.outcome) feedback.current?.focus();
    }, [update, refresh, tenant.id]);
    return (
        <>
            <div className="page-heading">
                <h1>{t("inbox")}</h1>
                <button
                    className="button secondary"
                    onClick={() => void refresh.requests(tenant.id)}
                >
                    {t("refreshList")}
                </button>
            </div>
            {update.outcome && (
                <div ref={feedback} tabIndex={-1}>
                    <Notice>
                        {t(
                            (
                                {
                                    approve: "approved",
                                    reject: "rejected",
                                    withdraw: "withdrawn",
                                    seen: "success",
                                } as const
                            )[update.outcome],
                        )}
                    </Notice>
                </div>
            )}
            {admin && (
                <div
                    className="button-group inbox-filters"
                    role="group"
                    aria-label={t("inbox")}
                >
                    {(["pending", "history", "mine"] as const).map((value) => (
                        <button
                            key={value}
                            className={`button ${effectiveScope === value ? "" : "secondary"}`}
                            aria-pressed={effectiveScope === value}
                            onClick={() => {
                                setScope(value);
                                setPage(1);
                            }}
                        >
                            {t(value)}
                        </button>
                    ))}
                </div>
            )}
            <InboxPage
                key={`${effectiveScope}:${page}`}
                tenantId={tenant.id}
                scope={effectiveScope}
                page={page}
                onChanged={changed}
                onPage={setPage}
            />
        </>
    );
}

function InboxPage({
    tenantId,
    scope,
    page,
    onChanged,
    onPage,
}: {
    tenantId: number;
    scope: Scope;
    page: number;
    onChanged: (action: Decision) => void;
    onPage: (page: number) => void;
}) {
    const t = useTranslations("ChangeRequests");
    const path = { tenant_id: tenantId };
    const base = buildUrl("/api/tenants/{tenant_id}/change-requests/", {
        path,
    });
    const items = useApiQuery(
        "/api/tenants/{tenant_id}/change-requests/",
        { path, query: { scope, page } },
        { poll: cadence.live },
    );
    const [opened, setOpened] = useState<number | null>(null);
    function go(url: string | null) {
        if (!url) return;
        const target = new URL(url, window.location.origin);
        const number = Number(target.searchParams.get("page") ?? 1);
        if (
            target.pathname === base &&
            Number.isSafeInteger(number) &&
            number > 0
        )
            onPage(number);
    }
    return (
        <>
            {items.loading && <p role="status">{t("loading")}</p>}
            <RefreshFailed resource={items} />
            {items.error && !items.data && <Notice error>{items.error}</Notice>}
            {items.data && !items.data.results.length && (
                <section className="panel">
                    <p>{t("empty")}</p>
                </section>
            )}
            <div className="inbox-list">
                {items.data?.results.map((item) => (
                    <article className="panel inbox-card" key={item.id}>
                        <div className="section-heading">
                            <div>
                                <p className="eyebrow">{item.plant_name}</p>
                                <h2>
                                    {t(
                                        item.kind === "production"
                                            ? "production"
                                            : "incident",
                                    )}
                                </h2>
                            </div>
                            <Tag
                                tone={
                                    item.status === "approved"
                                        ? "success"
                                        : item.status === "rejected"
                                          ? "danger"
                                          : item.status === "pending"
                                            ? "warning"
                                            : "neutral"
                                }
                            >
                                <>{t(item.status)}</>
                            </Tag>
                        </div>
                        {item.unread && (
                            <p className="inbox-unread">{t("unread")}</p>
                        )}
                        <p>
                            {t("submitter", { name: item.proposed_by.name })}
                            {item.former_member
                                ? ` · ${t("formerMember")}`
                                : ""}
                        </p>
                        <Triage item={item} />
                        <SubmissionTime value={item.created_at} />
                        <button
                            className="button secondary"
                            aria-expanded={opened === item.id}
                            aria-controls={`request-${item.id}`}
                            onClick={() =>
                                setOpened(opened === item.id ? null : item.id)
                            }
                        >
                            {opened === item.id ? t("close") : t("details")}
                        </button>
                        {opened === item.id && (
                            <RequestDetail
                                key={item.id}
                                id={item.id}
                                tenantId={tenantId}
                                onChanged={onChanged}
                            />
                        )}
                    </article>
                ))}
            </div>
            {(page > 1 || items.data?.next) && (
                <nav
                    className="button-group inbox-pagination"
                    aria-label={t("pages")}
                >
                    <button
                        className="button secondary"
                        disabled={page === 1}
                        onClick={() => onPage(page - 1)}
                    >
                        {t("previous")}
                    </button>
                    <p>{t("page", { page })}</p>
                    <button
                        className="button secondary"
                        disabled={!items.data?.next}
                        onClick={() => go(items.data?.next ?? null)}
                    >
                        {t("next")}
                    </button>
                </nav>
            )}
        </>
    );
}

function Triage({ item }: { item: Summary }) {
    const t = useTranslations("ChangeRequests");
    const locale = useLocale();
    const time = new Intl.DateTimeFormat(locale, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: item.timezone,
    });
    return (
        <>
            <p>
                <time dateTime={item.period_start}>
                    {time.format(new Date(item.period_start))}
                </time>{" "}
                –{" "}
                {item.period_end ? (
                    <time dateTime={item.period_end}>
                        {time.format(new Date(item.period_end))}
                    </time>
                ) : (
                    t("ongoing")
                )}
                {item.kind === "production" &&
                    ` · ${t("intervals", { count: item.item_count })}`}
            </p>
            {item.incident_action && (
                <p>
                    {t(item.incident_action)}: {item.incident_title}
                </p>
            )}
        </>
    );
}

function SubmissionTime({ value }: { value: string }) {
    const t = useTranslations("ChangeRequests");
    const format = useFormatter();
    return (
        <p className="fine">
            {t("submittedAt", {
                time: format.dateTime(new Date(value), {
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                    timeZoneName: "short",
                }),
            })}
        </p>
    );
}

function RequestDetail({
    id,
    tenantId,
    onChanged,
}: {
    id: number;
    tenantId: number;
    onChanged: (action: Decision) => void;
}) {
    const t = useTranslations("ChangeRequests");
    const path = { tenant_id: tenantId, request_id: id };
    const resource = useApiQuery(
        "/api/tenants/{tenant_id}/change-requests/{request_id}/",
        { path },
    );
    const [state, setState] = useState<
        { status: "idle" | "pending" } | { status: "error"; message: string }
    >({ status: "idle" });
    const [blockedContext, setBlockedContext] = useState<ChangeRequest | null>(
        null,
    );
    const heading = useRef<HTMLHeadingElement>(null);
    const item = resource.data;
    const reviewBlocked = item !== undefined && blockedContext === item;
    useEffect(() => {
        heading.current?.focus();
    }, []);
    async function act(action: Decision, run: () => Promise<unknown>) {
        if (state.status === "pending") return;
        setState({ status: "pending" });
        try {
            await run();
            setState({ status: "idle" });
            onChanged(action);
        } catch (error) {
            if (action === "approve") setBlockedContext(item ?? null);
            setState({ status: "error", message: (error as Error).message });
            throw error;
        }
    }
    const pending = state.status === "pending";
    return (
        <section
            id={`request-${id}`}
            className="inbox-detail"
            aria-busy={pending}
        >
            <h3 ref={heading} tabIndex={-1}>
                {t("details")}
            </h3>
            {resource.loading && <p role="status">{t("loading")}</p>}
            {resource.error && <Notice error>{resource.error}</Notice>}
            {state.status === "error" && <Notice error>{state.message}</Notice>}
            <button
                className="button secondary"
                disabled={pending}
                onClick={() => {
                    setBlockedContext(item ?? null);
                    setState({ status: "idle" });
                    resource.retry();
                }}
            >
                {t("refreshDetails")}
            </button>
            {item && (
                <>
                    <p className="fine">
                        {t("configuration", { id: item.configuration_id })} ·{" "}
                        {item.timezone}
                    </p>
                    {item.reason && (
                        <p className="submission-reason">{item.reason}</p>
                    )}
                    {item.production_items.map((row) => (
                        <ProductionProposal
                            key={row.id}
                            row={row}
                            timezone={item.timezone}
                            closed={
                                item.status === "approved" ||
                                item.status === "rejected"
                            }
                        />
                    ))}
                    {item.incident && (
                        <IncidentProposal
                            incident={item.incident}
                            pending={item.status === "pending"}
                            timezone={item.timezone}
                        />
                    )}
                    {item.conflict && (
                        <Notice error>
                            {t("conflict")} {item.conflict.message}
                        </Notice>
                    )}
                    {reviewBlocked && (
                        <Notice error>{t("contextChanged")}</Notice>
                    )}
                    {item.decision_reason && (
                        <p>
                            {t("decisionReason", {
                                reason: item.decision_reason,
                            })}
                        </p>
                    )}
                    {item.can_review && item.status === "pending" && (
                        <div className="inbox-decisions">
                            <button
                                className="button"
                                disabled={
                                    pending ||
                                    !!item.conflict ||
                                    !item.review_fingerprint ||
                                    reviewBlocked ||
                                    !!resource.error
                                }
                                onClick={() => {
                                    const fingerprint = item.review_fingerprint;
                                    if (fingerprint)
                                        void act("approve", () =>
                                            api.post(
                                                "/api/tenants/{tenant_id}/change-requests/{request_id}/approve/",
                                                {
                                                    path,
                                                    body: {
                                                        review_fingerprint:
                                                            fingerprint,
                                                    },
                                                },
                                            ),
                                        ).catch(() => {});
                                }}
                            >
                                {t("approve")}
                            </button>
                            <ActionForm
                                permitted={!pending}
                                submit={t("reject")}
                                onSubmit={async (data) => {
                                    const reason = String(
                                        data.get("reason") ?? "",
                                    ).trim();
                                    if (!reason)
                                        throw new Error(t("rejectReason"));
                                    await act("reject", () =>
                                        api.post(
                                            "/api/tenants/{tenant_id}/change-requests/{request_id}/reject/",
                                            { path, body: { reason } },
                                        ),
                                    );
                                }}
                            >
                                <label className="field">
                                    {t("rejectReason")}
                                    <textarea
                                        required
                                        name="reason"
                                        maxLength={500}
                                    />
                                </label>
                            </ActionForm>
                        </div>
                    )}
                    {item.can_withdraw && (
                        <>
                            <p className="fine">{t("withdrawHelp")}</p>
                            <button
                                className="button secondary"
                                disabled={pending}
                                onClick={() => {
                                    void act("withdraw", () =>
                                        api.post(
                                            "/api/tenants/{tenant_id}/change-requests/{request_id}/withdraw/",
                                            { path },
                                        ),
                                    ).catch(() => {});
                                }}
                            >
                                {t("withdraw")}
                            </button>
                        </>
                    )}
                    {item.unread && item.status !== "pending" && (
                        <button
                            className="button secondary"
                            disabled={pending}
                            onClick={() => {
                                void act("seen", () =>
                                    api.post(
                                        "/api/tenants/{tenant_id}/change-requests/{request_id}/seen/",
                                        { path },
                                    ),
                                ).catch(() => {});
                            }}
                        >
                            {t("markSeen")}
                        </button>
                    )}
                </>
            )}
        </section>
    );
}

function ProductionProposal({
    row,
    timezone,
    closed,
}: {
    row: ChangeRequest["production_items"][number];
    timezone: string;
    closed: boolean;
}) {
    const t = useTranslations("ChangeRequests");
    const format = useFormatter();
    const locale = useLocale();
    const evidenceTime = new Intl.DateTimeFormat(locale, {
        timeZone: timezone,
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "shortOffset",
    });
    return (
        <section className="proposal-comparison">
            <h4>
                {new Intl.DateTimeFormat(locale, {
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                    timeZone: timezone,
                    timeZoneName: "shortOffset",
                }).format(new Date(row.starts_at))}{" "}
                –{" "}
                {new Intl.DateTimeFormat(locale, {
                    hour: "2-digit",
                    minute: "2-digit",
                    timeZone: timezone,
                    timeZoneName: "shortOffset",
                }).format(new Date(row.ends_at))}
            </h4>
            {row.action === "revert" && <p>{t("revert")}</p>}
            <div className="proposal-values">
                <div>
                    <h5>{t(closed ? "atDecision" : "current")}</h5>
                    <ReadingValue value={row.current} />
                </div>
                <div>
                    <h5>{t("proposed")}</h5>
                    <ReadingValue value={row.proposed} />
                </div>
            </div>
            <p className="fine">
                {t("atSubmission")}:{" "}
                {row.baseline_energy_mwh === null
                    ? t("noReading")
                    : `${format.number(row.baseline_energy_mwh, { maximumFractionDigits: 6 })} MWh`}
            </p>
            {(row.baseline_energy_mwh !== (row.current?.energy_mwh ?? null) ||
                row.baseline_source_kind !==
                    (row.current?.source_kind ?? "")) && (
                <Notice>{t("currentChanged")}</Notice>
            )}
            {(row.warnings ?? []).map((warning) => (
                <Notice key={`${warning.code}:${warning.starts_at}`}>
                    {t("providerHourIncomplete", {
                        start: evidenceTime.format(new Date(warning.starts_at)),
                        end: evidenceTime.format(new Date(warning.ends_at)),
                    })}
                </Notice>
            ))}
            {!!row.provider_intervals?.length && (
                <details>
                    <summary>{t("automaticIntervals")}</summary>
                    <ul>
                        {row.provider_intervals.map((interval) => (
                            <li
                                key={`${interval.starts_at}:${interval.ends_at}`}
                            >
                                {evidenceTime.format(
                                    new Date(interval.starts_at),
                                )}{" "}
                                –{" "}
                                {evidenceTime.format(
                                    new Date(interval.ends_at),
                                )}
                                :{" "}
                                {format.number(interval.energy_mwh, {
                                    maximumFractionDigits: 6,
                                })}{" "}
                                MWh
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </section>
    );
}

function ReadingValue({ value }: { value: Reading | null }) {
    const t = useTranslations("ChangeRequests");
    const format = useFormatter();
    const label = (value: string) =>
        t.has(value as Parameters<typeof t>[0])
            ? t(value as Parameters<typeof t>[0])
            : value;
    return value ? (
        <>
            <p className="proposal-energy">
                {format.number(value.energy_mwh, { maximumFractionDigits: 6 })}{" "}
                MWh
            </p>
            <p>
                {label(value.quality)} ·{" "}
                {value.source_kind === "manual" ? t("manual") : t("provider")}
            </p>
        </>
    ) : (
        <p>{t("noReading")}</p>
    );
}

function IncidentProposal({
    incident,
    pending,
    timezone,
}: {
    incident: NonNullable<ChangeRequest["incident"]>;
    pending: boolean;
    timezone: string;
}) {
    const t = useTranslations("ChangeRequests");
    const format = useFormatter();
    const locale = useLocale();
    const before = incident.before;
    const proposed =
        incident.action === "create" || incident.action === "update"
            ? incident.values
            : before;
    const fields = [
        "title",
        "kind",
        "starts_at",
        "ends_at",
        "available_dc_mw",
        "available_inverter_mw",
        "available_export_mw",
        "notes",
    ] as const;
    const show = (
        name: (typeof fields)[number],
        value: string | number | null | undefined,
    ) => {
        if (value === null || value === undefined)
            return name === "ends_at" ? t("ongoing") : t("unknown");
        if (typeof value === "number")
            return format.number(value, { maximumFractionDigits: 6 });
        if (name === "starts_at" || name === "ends_at")
            return new Intl.DateTimeFormat(locale, {
                year: "numeric",
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                timeZone: timezone,
                timeZoneName: "shortOffset",
            }).format(new Date(value));
        return name === "kind" && t.has(value as Parameters<typeof t>[0])
            ? t(value as Parameters<typeof t>[0])
            : value || "—";
    };
    return (
        <section className="proposal-comparison">
            <h4>{t(incident.action)}</h4>
            {pending &&
                (incident.action === "create" ||
                    incident.action === "update") &&
                !proposed?.ends_at && <Notice>{t("openImpact")}</Notice>}
            {incident.action === "cancel" ? (
                <p>{t("cancel")}</p>
            ) : (
                <dl className="incident-proposal-values">
                    {fields.map((field) => {
                        const proposedValue =
                            field === "ends_at" && incident.action === "resolve"
                                ? incident.ends_at
                                : proposed?.[field];
                        const label =
                            field === "starts_at"
                                ? "starts"
                                : field === "ends_at"
                                  ? "ends"
                                  : field;
                        return (
                            <div key={field}>
                                <dt>{t(label)}</dt>
                                <dd>
                                    {before && (
                                        <span>
                                            <strong>
                                                {t("beforeChange")}:
                                            </strong>{" "}
                                            {show(field, before[field])}
                                        </span>
                                    )}
                                    <span>
                                        <strong>{t("proposed")}:</strong>{" "}
                                        {show(field, proposedValue)}
                                    </span>
                                </dd>
                            </div>
                        );
                    })}
                </dl>
            )}
        </section>
    );
}
