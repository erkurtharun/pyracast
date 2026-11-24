const FIXTURES = new Set(["page", "context", "browser"]);
const PAGE_SIDE = new Set([
    "evaluate",
    "evaluateHandle",
    "evaluateAll",
    "addInitScript",
    "waitForFunction",
    "$eval",
    "$$eval",
]);

const property = (node) =>
    node.type === "MemberExpression" && !node.computed
        ? node.property.name
        : undefined;

function root(node) {
    let current = node;
    while (
        current.type === "MemberExpression" ||
        current.type === "CallExpression"
    )
        current =
            current.type === "CallExpression" ? current.callee : current.object;
    return current.type === "Identifier" ? current.name : undefined;
}

function hasKey(node, name, value) {
    return (
        node?.type === "ObjectExpression" &&
        node.properties.some(
            (entry) =>
                entry.type === "Property" &&
                !entry.computed &&
                (entry.key.name ?? entry.key.value) === name &&
                (value === undefined ||
                    (entry.value.type === "Literal" &&
                        entry.value.value === value)),
        )
    );
}

const functionTypes = new Set([
    "ArrowFunctionExpression",
    "FunctionExpression",
]);

export const browserClock = {
    meta: {
        type: "problem",
        messages: {
            sharedFixture:
                "A browser case takes test from tests/support/test, whose clock starts every case at the shared instant.",
            clockTime:
                "Install a clock with a time; without one it starts at the real date.",
            realClock:
                "Date.now() and new Date() read the real date here; derive dates from the shared instant and measure elapsed time with performance.now().",
            ownContext:
                "A context of its own runs scripts on the real date; use the context fixture or one without JavaScript.",
        },
        schema: [],
    },
    create(context) {
        let directTest;
        const pageSide = (node) =>
            context.sourceCode
                .getAncestors(node)
                .some(
                    (ancestor) =>
                        functionTypes.has(ancestor.type) &&
                        ancestor.parent?.type === "CallExpression" &&
                        ancestor.parent.arguments.includes(ancestor) &&
                        PAGE_SIDE.has(property(ancestor.parent.callee)),
                );
        const readsNow = (node) => {
            if (!pageSide(node))
                context.report({ node, messageId: "realClock" });
        };
        return {
            ImportDeclaration(node) {
                if (
                    node.source.value !== "@playwright/test" ||
                    node.importKind === "type"
                )
                    return;
                for (const specifier of node.specifiers)
                    if (
                        specifier.type === "ImportSpecifier" &&
                        specifier.importKind !== "type" &&
                        specifier.imported.name === "test"
                    )
                        directTest = specifier.local.name;
            },
            CallExpression(node) {
                const name = property(node.callee);
                if (
                    name === "install" &&
                    property(node.callee.object) === "clock"
                ) {
                    if (!hasKey(node.arguments[0], "time"))
                        context.report({ node, messageId: "clockTime" });
                } else if (
                    name === "newContext" ||
                    (name === "newPage" && root(node.callee) === "browser")
                ) {
                    if (!hasKey(node.arguments[0], "javaScriptEnabled", false))
                        context.report({ node, messageId: "ownContext" });
                } else if (name === "now" && root(node.callee) === "Date") {
                    readsNow(node);
                } else if (
                    node.callee.type === "Identifier" &&
                    node.callee.name === "Date"
                ) {
                    readsNow(node);
                }
                if (
                    directTest === undefined ||
                    root(node.callee) !== directTest
                )
                    return;
                for (const argument of node.arguments) {
                    const [fixtures] = functionTypes.has(argument.type)
                        ? argument.params
                        : [];
                    if (
                        fixtures?.type === "ObjectPattern" &&
                        fixtures.properties.some(
                            (entry) =>
                                entry.type === "Property" &&
                                FIXTURES.has(entry.key.name),
                        )
                    )
                        context.report({
                            node: fixtures,
                            messageId: "sharedFixture",
                        });
                }
            },
            NewExpression(node) {
                if (
                    node.callee.type === "Identifier" &&
                    node.callee.name === "Date" &&
                    node.arguments.length === 0
                )
                    readsNow(node);
            },
        };
    },
};
