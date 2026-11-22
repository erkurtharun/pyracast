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

function messageKeys(node: unknown, prefix = ""): string[] {
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

const CODE = /^[a-z][a-z0-9_]*$/;
const COMPOSED_CODE = /^(?:[a-z0-9_]|\{[\w.]+\})*_(?:[a-z0-9_]|\{[\w.]+\})*$/;
// A comment, or a string literal with its prefix and its whole text; a docstring is one literal,
// so a code it mentions is not a literal of its own.
const PYTHON_TOKEN =
    /#[^\n]*|([A-Za-z]{0,2})("""|'''|"|')((?:\\[\s\S]|(?!\2)[^\\])*)\2/g;

function pythonCodes(
    text: string,
    literals: Set<string>,
    composed: Set<string>,
) {
    for (const [, prefix, , value] of text.matchAll(PYTHON_TOKEN)) {
        if (prefix === undefined || value === undefined) continue;
        if (!/f/i.test(prefix)) {
            if (CODE.test(value)) literals.add(value);
        } else if (value.includes("{") && COMPOSED_CODE.test(value))
            composed.add(value.replace(/\{[\w.]+\}/g, "[a-z0-9_]+"));
    }
}

function webCodes(file: string, literals: Set<string>) {
    const source = ts.createSourceFile(
        file,
        readFileSync(file, "utf8"),
        ts.ScriptTarget.Latest,
    );
    ts.forEachChild(source, function visit(node): void {
        if (ts.isStringLiteralLike(node) && CODE.test(node.text))
            literals.add(node.text);
        ts.forEachChild(node, visit);
    });
}

type EnumerationMembers = {
    module: string;
    enumeration: string;
    definedIn: string;
};
type FirstArguments = {
    module: string;
    call: string;
};
type Producer = string | EnumerationMembers | FirstArguments;
export type Producers = Readonly<Record<string, readonly Producer[]>>;

// The modules, from the repository root, whose literals can produce each code family, whose
// code names members of an enumeration, or whose calls of a result type pass the code first;
// a code another module holds labels nothing.
const PRODUCERS: Producers = {
    "Calibrations.values": ["src/pyracast/plants/numerics.py"],
    "Calibrations.status_messages": [
        { module: "src/pyracast/plants/numerics.py", call: "FitResult" },
        {
            module: "src/pyracast/plants/calibration_runs.py",
            enumeration: "CalibrationStatus",
            definedIn: "src/pyracast/plants/models.py",
        },
        {
            module: "src/pyracast/plants/training.py",
            enumeration: "CalibrationStatus",
            definedIn: "src/pyracast/plants/models.py",
        },
    ],
    "Calibrations.reasons": ["src/pyracast/plants/numerics.py"],
    "CalibrationAutomation.reasons": [
        "src/pyracast/plants/automation.py",
        "src/pyracast/plants/calibration_runs.py",
        {
            module: "src/pyracast/plants/activation.py",
            enumeration: "ErrorCode",
            definedIn: "src/pyracast/errors.py",
        },
    ],
    "ForecastPerformance.reasons": ["src/pyracast/plants/performance.py"],
    "Forecast.qualityFlags": [
        "src/pyracast/calculation.py",
        "src/pyracast/forecasts/intraday.py",
        "src/pyracast/forecasts/sources.py",
        "src/pyracast/irradiance/__init__.py",
        "src/pyracast/plants/automatic_forecast.py",
        "src/pyracast/plants/current_forecast.py",
        "src/pyracast/plants/forecast_sources.py",
        "src/pyracast/plants/forecasting.py",
        "src/pyracast/plants/history.py",
        "src/pyracast/plants/incident_forecasts.py",
        "src/pyracast/plants/numerics.py",
        "web/src/features/forecast/panel/model.ts",
    ],
    "Outages.proposalReasons": [
        {
            module: "src/pyracast/change_requests/policy.py",
            enumeration: "ErrorCode",
            definedIn: "src/pyracast/errors.py",
        },
        "web/src/features/plant/outage/model.ts",
    ],
    "ProductionHistory.reasons": ["src/pyracast/plants/production.py"],
};

const ENUMERATION_MEMBER = /^[ \t]+([A-Z][A-Z0-9_]*) = (["'])([a-z0-9_]+)\2/gm;

function enumerationValues(
    root: string,
    producer: EnumerationMembers,
): string[] {
    const definition = readFileSync(
        path.join(root, producer.definedIn),
        "utf8",
    );
    const body =
        new RegExp(
            `^class ${producer.enumeration}\\b.*\\n((?:[ \\t]+.*\\n?|\\n)*)`,
            "m",
        ).exec(definition)?.[1] ?? "";
    const values = new Map(
        [...body.matchAll(ENUMERATION_MEMBER)].map(([, member, , value]) => [
            member,
            value,
        ]),
    );
    const code = readFileSync(path.join(root, producer.module), "utf8").replace(
        PYTHON_TOKEN,
        " ",
    );
    const reference = new RegExp(
        `\\b${producer.enumeration}\\.([A-Z][A-Z0-9_]*)\\b`,
        "g",
    );
    return [...code.matchAll(reference)].flatMap(([, member]) => {
        const value = member === undefined ? undefined : values.get(member);
        return value === undefined ? [] : [value];
    });
}

// The text up to the first comma stands for the first argument, so a comma inside it can only
// drop a code.
function firstArgumentValues(root: string, producer: FirstArguments): string[] {
    const code = readFileSync(path.join(root, producer.module), "utf8");
    const literals = new Set<string>();
    const call = new RegExp(`\\b${producer.call}\\(([^,]*)`, "g");
    for (const [, argument] of code.matchAll(call))
        if (argument !== undefined) pythonCodes(argument, literals, new Set());
    return [...literals];
}

// Every lower-case code a string literal of the modules holds, every Python f-string that
// composes one, every enumeration value a producer's code names by its member, and every code
// a producer passes first to its result type.
function codePatterns(root: string, producers: readonly Producer[]): RegExp[] {
    const literals = new Set<string>();
    const composed = new Set<string>();
    for (const producer of producers) {
        if (typeof producer !== "string") {
            const values =
                "call" in producer
                    ? firstArgumentValues(root, producer)
                    : enumerationValues(root, producer);
            for (const value of values) literals.add(value);
            continue;
        }
        const file = path.join(root, producer);
        if (producer.endsWith(".py"))
            pythonCodes(readFileSync(file, "utf8"), literals, composed);
        else webCodes(file, literals);
    }
    return [...literals, ...composed].map((code) => new RegExp(`^${code}$`));
}

export function unusedKeys(
    web: string,
    messages: object,
    producers: Producers = PRODUCERS,
): string[] {
    const use = keyUse(web);
    const root = path.join(web, "..");
    const codes = new Map(
        [...use.families].map((family) => [
            family,
            codePatterns(root, producers[family] ?? []),
        ]),
    );
    return messageKeys(messages).filter((key) => {
        if (key.startsWith(`${ENUMS}.`) || use.used.has(key)) return false;
        const family = [...use.families].find((name) =>
            key.startsWith(`${name}.`),
        );
        const code = family && key.slice(family.length + 1);
        return (
            !code || !codes.get(family)?.some((pattern) => pattern.test(code))
        );
    });
}
