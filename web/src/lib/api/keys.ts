type Parameters = { path?: object; query?: object };

export const keys = {
    owner: (owner: string) => ["session", owner] as const,
    account: (owner: string) => [...keys.owner(owner), "account"] as const,
    workspace: (owner: string, tenant: number) =>
        [...keys.owner(owner), "tenant", tenant] as const,
    scope(owner: string, path: object = {}) {
        const tenant = (path as { tenant_id?: unknown }).tenant_id;
        return typeof tenant === "number"
            ? keys.workspace(owner, tenant)
            : keys.account(owner);
    },
    path: (owner: string, path: string, parameters: object = {}) =>
        [...keys.scope(owner, parameters), path, parameters] as const,
    request: (owner: string, path: string, options: Parameters = {}) =>
        [...keys.path(owner, path, options.path), options.query ?? {}] as const,
};
