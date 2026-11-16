"use client";
import { useEffect, useRef, useState } from "react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { request, type Tenant, type Page } from "@/lib/api";
import type { components } from "@/lib/schema";
import { ActionForm, Notice, Tag, useResource } from "./ui";
import { useChangeRequestUpdates } from "./change-request-updates";

type ChangeRequest = components["schemas"]["ChangeRequestOutput"];
type Summary = components["schemas"]["ChangeRequestSummaryOutput"];
type Reading = components["schemas"]["ProductionValueOutput"];
type Decision = "approve" | "reject" | "withdraw" | "seen";

export function OwnPendingRequests({
    tenantBase,
    plantId,
    revision,
}: {
    tenantBase: string;
    plantId: number;
    revision: number;
}) {
    const t = useTranslations("ChangeRequests");
    const items = useResource<Page<Summary>>(
        `${tenantBase}change-requests/?scope=pending&plant_id=${plantId}`,
        revision,
        30_000,
    );
    const count = items.data?.count ?? 0;
    if (!count) return null;
    return <Notice>{t("pendingContext", { count })}</Notice>;
}

export function OperationalInbox({
    tenant,
    token,
}: {
    tenant: Tenant;
    token: string;
}) {
    const t = useTranslations("ChangeRequests");
    const [scope, setScope] = useState<"pending" | "history" | "mine">(
        "pending",
    );
    const [update, setUpdate] = useState<{
        revision: number;
        outcome: Decision | null;
    }>({ revision: 0, outcome: null });
    const feedback = useRef<HTMLDivElement>(null);
    const [page, setPage] = useState(1);
    const admin = tenant.role === "admin";
    const effectiveScope = admin ? scope : "mine";
    const base = `/api/tenants/${tenant.id}/change-requests/`;
    const changed = (outcome: Decision | null = null) => {
        if (outcome && outcome !== "seen" && effectiveScope === "pending")
            setPage(1);
        setUpdate((value) => ({ revision: value.revision + 1, outcome }));
    };
    useEffect(() => {
        if (update.outcome) feedback.current?.focus();
    }, [update]);
    return (
        <>
            <div className="page-heading">
                <h1>{t("inbox")}</h1>
                <button className="button secondary" onClick={() => changed()}>
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
                base={base}
                scope={effectiveScope}
                page={page}
                revision={update.revision}
                token={token}
                onChanged={changed}
                onPage={setPage}
            />
        </>
    );
}

function InboxPage({
    base,
    scope,
    page,
    revision,
    token,
    onChanged,
    onPage,
}: {
    base: string;
    scope: string;
    page: number;
    revision: number;
    token: string;
    onChanged: (action: Decision) => void;
    onPage: (page: number) => void;
}) {
    const t = useTranslations("ChangeRequests");
    const items = useResource<Page<Summary>>(
        `${base}?scope=${scope}&page=${page}`,
        revision,
        30_000,
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
            {items.error && <Notice error>{items.error}</Notice>}
            {!items.loading && !items.error && !items.data?.results.length && (
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
                        <SubmissionTime value={item.submitted_at} />
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
                                base={base}
                                token={token}
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
    base,
    token,
    onChanged,
}: {
    id: number;
    base: string;
    token: string;
    onChanged: (action: Decision) => void;
}) {
    const t = useTranslations("ChangeRequests");
    const refreshCounters = useChangeRequestUpdates();
    const [revision, setRevision] = useState(0);
    const resource = useResource<ChangeRequest>(`${base}${id}/`, revision);
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
    async function act(action: Decision, body: unknown = {}) {
        if (state.status === "pending") return;
        setState({ status: "pending" });
        try {
            await request(`${base}${id}/${action}/`, "POST", body, token);
            setState({ status: "idle" });
            setRevision((value) => value + 1);
            onChanged(action);
            refreshCounters();
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
                    setRevision((value) => value + 1);
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
                            {t("conflict")} {item.conflict.detail}
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
                                    void act("approve", {
                                        review_fingerprint:
                                            item.review_fingerprint,
                                    }).catch(() => {});
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
                                    await act("reject", { reason });
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
                                    void act("withdraw").catch(() => {});
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
                                void act("seen").catch(() => {});
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
