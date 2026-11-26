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
// A worker reads the real date: the installed clock reaches page frames only.
const CLOCKED = new Set([
    "Page",
    "Frame",
    "Locator",
    "ElementHandle",
    "BrowserContext",
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

const keyOf = (entry) =>
    entry.type === "Property" && !entry.computed
        ? (entry.key.name ?? entry.key.value)
        : undefined;

function effective(node, name) {
    if (node?.type !== "ObjectExpression") return undefined;
    for (const entry of node.properties.toReversed()) {
        const key = keyOf(entry);
        if (key === undefined) return undefined;
        if (key === name) return entry.value;
    }
    return undefined;
}

function definite(node) {
    switch (node?.type) {
        case undefined:
            return false;
        case "Identifier":
            return node.name !== "undefined";
        case "Literal":
            return node.value !== null;
        case "UnaryExpression":
            return node.operator !== "void";
        case "TSAsExpression":
        case "TSSatisfiesExpression":
        case "TSNonNullExpression":
        case "TSTypeAssertion":
            return definite(node.expression);
        case "ConditionalExpression":
            return definite(node.consequent) && definite(node.alternate);
        case "LogicalExpression":
            return (
                definite(node.right) &&
                (node.operator !== "&&" || definite(node.left))
            );
        default:
            return true;
    }
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
                "Install a clock at an explicit time; without one it starts at the real date.",
            realClock:
                "Date.now() and new Date() read the real date here; derive dates from the shared instant and measure elapsed time with performance.now().",
            ownContext:
                "A context of its own runs scripts on the real date; use the context fixture or one whose last word on javaScriptEnabled is false.",
            clockFixture:
                "Name each option of the shared test and leave its clock fixture alone; it starts every case at the shared instant.",
        },
        schema: [],
    },
    create(context) {
        let directTest;
        const sharedTests = new Set();
        const services = context.sourceCode.parserServices;
        const clocked = (receiver) => {
            if (!services?.program) return false;
            const type = services.getTypeAtLocation(receiver);
            return (type.isUnion() ? type.types : [type]).every((part) => {
                const symbol = part.getSymbol();
                return (
                    CLOCKED.has(symbol?.name) &&
                    symbol.declarations?.some((declaration) =>
                        declaration
                            .getSourceFile()
                            .fileName.includes("/playwright-core/"),
                    )
                );
            });
        };
        const pageSide = (node) =>
            context.sourceCode
                .getAncestors(node)
                .some(
                    (ancestor) =>
                        functionTypes.has(ancestor.type) &&
                        ancestor.parent?.type === "CallExpression" &&
                        ancestor.parent.arguments.includes(ancestor) &&
                        PAGE_SIDE.has(property(ancestor.parent.callee)) &&
                        clocked(ancestor.parent.callee.object),
                );
        const readsNow = (node) => {
            if (!pageSide(node))
                context.report({ node, messageId: "realClock" });
        };
        return {
            ImportDeclaration(node) {
                const source = node.source.value;
                if (
                    typeof source !== "string" ||
                    !source.endsWith("/test") ||
                    node.importKind === "type"
                )
                    return;
                for (const specifier of node.specifiers)
                    if (
                        specifier.type === "ImportSpecifier" &&
                        specifier.importKind !== "type" &&
                        specifier.imported.name === "test"
                    ) {
                        if (source === "@playwright/test")
                            directTest = specifier.local.name;
                        else sharedTests.add(specifier.local.name);
                    }
            },
            CallExpression(node) {
                const name = property(node.callee);
                if (
                    name === "install" &&
                    property(node.callee.object) === "clock"
                ) {
                    if (!definite(effective(node.arguments[0], "time")))
                        context.report({ node, messageId: "clockTime" });
                } else if (
                    name === "newContext" ||
                    (name === "newPage" && root(node.callee) === "browser")
                ) {
                    const scripts = effective(
                        node.arguments[0],
                        "javaScriptEnabled",
                    );
                    if (scripts?.type !== "Literal" || scripts.value !== false)
                        context.report({ node, messageId: "ownContext" });
                } else if (
                    (name === "use" || name === "extend") &&
                    sharedTests.has(root(node.callee))
                ) {
                    if (
                        node.arguments.some(
                            (argument) =>
                                argument.type !== "ObjectExpression" ||
                                argument.properties.some((entry) =>
                                    [undefined, "clock"].includes(keyOf(entry)),
                                ),
                        )
                    )
                        context.report({ node, messageId: "clockFixture" });
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
