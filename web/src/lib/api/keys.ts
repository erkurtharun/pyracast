type Parameters = { path?: object; query?: object };

export const keys = {
    user: (user: number) => ["user", user] as const,
    account: (user: number) => [...keys.user(user), "account"] as const,
    workspace: (user: number, tenant: number) =>
        [...keys.user(user), "tenant", tenant] as const,
    scope(user: number, path: object = {}) {
        const tenant = (path as { tenant_id?: unknown }).tenant_id;
        return typeof tenant === "number"
            ? keys.workspace(user, tenant)
            : keys.account(user);
    },
    path: (user: number, path: string, parameters: object = {}) =>
        [...keys.scope(user, parameters), path, parameters] as const,
    request: (
        user: number,
        path: string,
        options: Parameters = {},
        locale = "",
    ) =>
        [
            ...keys.path(user, path, options.path),
            options.query ?? {},
            locale,
        ] as const,
};
