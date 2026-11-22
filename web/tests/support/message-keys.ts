import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import ts from "typescript";

// Calls that return a translator bound to their first argument's namespace; `useCodeLabel`
// returns one for codes the server or a screen sends.
const FACTORIES = new Set([
    "useTranslations",
    "getTranslations",
    "useCodeLabel",
]);
const CODE_FACTORY = "useCodeLabel";
const FIXED: Record<string, string> = { browserErrors: "Errors" };
const METHODS = new Set(["rich", "markup", "raw", "has"]);
const CATALOGUE = /[\\/]messages[\\/](?:en|tr)[\\/][\w-]+\.json$/;
// Checked against the generated enumerations by vocabulary.spec.ts.
const ENUMS = "Enums";

export type KeyUse = {
    used: Set<string>;
    families: Set<string>;
    unresolved: string[];
};

function files(directory: string, extension: RegExp): string[] {
    return readdirSync(directory, { recursive: true, withFileTypes: true })
        .filter((entry) => entry.isFile() && extension.test(entry.name))
        .map((entry) => path.join(entry.parentPath, entry.name));
}

export function messageKeys(node: unknown, prefix = ""): string[] {
    return node && typeof node === "object"
        ? Object.entries(node).flatMap(([key, value]) =>
              messageKeys(value, `${prefix}${key}.`),
          )
        : [prefix.slice(0, -1)];
}

function enclosingFunction(node: ts.Node): string | undefined {
    for (let parent = node.parent; parent; parent = parent.parent)
        if (ts.isFunctionDeclaration(parent)) return parent.name?.text;
    return undefined;
}

export function keyUse(web: string): KeyUse {
    const sources = files(path.join(web, "src"), /^(?!.*\.d\.ts$).*\.tsx?$/);
    const program = ts.createProgram(sources, {
        strict: true,
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.ESNext,
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        resolveJsonModule: true,
        skipLibCheck: true,
        noEmit: true,
        paths: { "@/*": ["./src/*"] },
        baseUrl: web,
    });
    const checker = program.getTypeChecker();
    const own = program
        .getSourceFiles()
        .filter((file) => sources.includes(path.normalize(file.fileName)));
    const where = (node: ts.Node) => {
        const file = node.getSourceFile();
        const line = file.getLineAndCharacterOfPosition(node.getStart()).line;
        return `${path.relative(web, file.fileName)}:${line + 1}`;
    };
    const result: KeyUse = {
        used: new Set(),
        families: new Set(),
        unresolved: [],
    };
    const bound = new Map<ts.Symbol, string>();
    const namespaceOf = (node: ts.Node) => {
        const target = checker.getSymbolAtLocation(node);
        return target && bound.get(target);
    };
    const factoryNamespace = (call: ts.Expression) => {
        if (!ts.isCallExpression(call) || !ts.isIdentifier(call.expression))
            return undefined;
        const name = call.expression.text;
        const argument = call.arguments[0];
        if (name in FIXED) return FIXED[name];
        if (!FACTORIES.has(name)) return undefined;
        if (argument && ts.isStringLiteralLike(argument)) {
            if (name === CODE_FACTORY) result.families.add(argument.text);
            return argument.text;
        }
        if (enclosingFunction(call) !== CODE_FACTORY)
            result.unresolved.push(`${where(call)} namespace`);
        return undefined;
    };
    const literals = (node: ts.Expression): string[] | undefined => {
        if (ts.isStringLiteralLike(node)) return [node.text];
        if (ts.isTemplateExpression(node)) {
            let texts = [node.head.text];
            for (const span of node.templateSpans) {
                const parts = literals(span.expression);
                if (!parts) return undefined;
                texts = texts.flatMap((text) =>
                    parts.map((part) => text + part + span.literal.text),
                );
            }
            return texts;
        }
        const type = checker.getTypeAtLocation(node);
        const members = type.isUnion() ? type.types : [type];
        return members.every((member) => member.isStringLiteral())
            ? members.map((member) => member.value)
            : undefined;
    };

    for (const file of own)
        ts.forEachChild(file, function visit(node): void {
            if (
                ts.isVariableDeclaration(node) &&
                ts.isIdentifier(node.name) &&
                node.initializer
            ) {
                const call = ts.isAwaitExpression(node.initializer)
                    ? node.initializer.expression
                    : node.initializer;
                const namespace = factoryNamespace(call);
                const declared = checker.getSymbolAtLocation(node.name);
                if (namespace !== undefined && declared)
                    bound.set(declared, namespace);
            }
            ts.forEachChild(node, visit);
        });

    // A translator handed to a function binds that function's parameter to its namespace.
    let size = -1;
    while (bound.size !== size) {
        size = bound.size;
        for (const file of own)
            ts.forEachChild(file, function visit(node): void {
                if (ts.isCallExpression(node))
                    node.arguments.forEach((argument, index) => {
                        const namespace = namespaceOf(argument);
                        if (namespace === undefined) return;
                        const declaration =
                            checker.getResolvedSignature(node)?.declaration;
                        const parameter =
                            declaration && !ts.isJSDocSignature(declaration)
                                ? declaration.parameters[index]
                                : undefined;
                        const target =
                            parameter &&
                            checker.getSymbolAtLocation(parameter.name);
                        if (target && !bound.has(target))
                            bound.set(target, namespace);
                    });
                ts.forEachChild(node, visit);
            });
    }

    for (const file of own)
        ts.forEachChild(file, function visit(node): void {
            if (ts.isCallExpression(node)) {
                let callee = node.expression;
                if (
                    ts.isPropertyAccessExpression(callee) &&
                    METHODS.has(callee.name.text)
                )
                    callee = callee.expression;
                const namespace = ts.isCallExpression(callee)
                    ? factoryNamespace(callee)
                    : namespaceOf(callee);
                const argument = node.arguments[0];
                if (namespace !== undefined && argument) {
                    const keys = literals(argument);
                    if (keys)
                        for (const key of keys)
                            result.used.add(`${namespace}.${key}`);
                    else if (
                        !result.families.has(namespace) &&
                        namespace !== ENUMS
                    )
                        result.unresolved.push(`${where(node)} ${namespace}`);
                }
            }
            // A catalogue imported as JSON names its keys by property access.
            if (
                ts.isPropertyAccessExpression(node) &&
                !ts.isPropertyAccessExpression(node.parent)
            ) {
                const names: string[] = [];
                let root: ts.Expression = node;
                while (ts.isPropertyAccessExpression(root)) {
                    names.unshift(root.name.text);
                    root = root.expression;
                }
                const declaration =
                    checker.getSymbolAtLocation(root)?.declarations?.[0];
                if (
                    declaration &&
                    ts.isImportClause(declaration) &&
                    ts.isStringLiteral(declaration.parent.moduleSpecifier) &&
                    CATALOGUE.test(declaration.parent.moduleSpecifier.text)
                )
                    result.used.add(names.join("."));
            }
            ts.forEachChild(node, visit);
        });
    return result;
}

// The codes a family can label: every lower-case code the backend or a screen writes as a
// string, and every Python f-string that composes one.
export function codePatterns(root: string): RegExp[] {
    const literals = new Set<string>();
    const composed = new Set<string>();
    for (const file of files(path.join(root, "src", "pyracast"), /\.py$/)) {
        const text = readFileSync(file, "utf8");
        for (const match of text.matchAll(
            /(?<![\w])["']([a-z][a-z0-9_]*)["']/g,
        ))
            literals.add(match[1]!);
        for (const match of text.matchAll(
            /\bf["']((?:[a-z0-9_]|\{[\w.]+\})*_(?:[a-z0-9_]|\{[\w.]+\})*)["']/g,
        ))
            if (match[1]!.includes("{"))
                composed.add(match[1]!.replace(/\{[\w.]+\}/g, "[a-z0-9_]+"));
    }
    for (const file of files(path.join(root, "web", "src"), /\.tsx?$/))
        for (const match of readFileSync(file, "utf8").matchAll(
            /["']([a-z][a-z0-9_]*)["']/g,
        ))
            literals.add(match[1]!);
    return [...literals, ...composed].map((code) => new RegExp(`^${code}$`));
}

export function unusedKeys(web: string, messages: object): string[] {
    const use = keyUse(web);
    const codes = codePatterns(path.join(web, ".."));
    return messageKeys(messages).filter((key) => {
        if (key.startsWith(`${ENUMS}.`) || use.used.has(key)) return false;
        const family = [...use.families].find((name) =>
            key.startsWith(`${name}.`),
        );
        const code = family && key.slice(family.length + 1);
        return !code || !codes.some((pattern) => pattern.test(code));
    });
}
