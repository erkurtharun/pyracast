import { browserClock } from "./browser-clock.mjs";

// Colours and spacing come only from src/styles/tokens.{css,ts}; the config switches this rule off there.
const HEX = /(?:^|[\s(,:])#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![\w-])/i;
const COLOUR_FUNCTION =
    /(?<![\w-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/i;
const NAMED_COLOUR = new RegExp(
    `(?<![\\w-])(?:${[
        "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown",
        "burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan",
        "darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid",
        "darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet",
        "deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro",
        "ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki",
        "lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow",
        "lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray",
        "lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine",
        "mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen",
        "mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace",
        "olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred",
        "papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue",
        "saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey",
        "snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow",
        "yellowgreen",
    ]
        .join(" ")
        .split(" ")
        .join("|")})(?![\\w-])`,
    "i",
);
const COLOUR_PROPERTY =
    /^(?:color|background|background-color|border|border-(?:top|right|bottom|left|block|inline)(?:-(?:start|end))?(?:-color)?|border-color|outline|outline-color|fill|stroke|box-shadow|text-shadow|text-decoration|text-decoration-color|caret-color|accent-color|column-rule|column-rule-color|stop-color|flood-color|lighting-color)$/;
const SPACING =
    /^(?:margin|padding|gap|row-gap|column-gap|inset|scroll-margin|scroll-padding)(?:-|$)/;
const STYLE_SPACING =
    /^(?:margin|padding|gap|rowGap|columnGap|inset|scrollMargin|scrollPadding)(?:[A-Z]|$)/;
// Percentages stay allowed: they size spacing relative to the containing block, not on the token scale.
const LENGTH =
    /(?<![\w.-])-?(?:\d*\.\d+|\d+)(?:px|rem|em|ch|ex|lh|vw|vh|vi|vb|vmin|vmax|svh|lvh|dvh|svw|lvw|dvw)\b/g;

function literalLength(value) {
    return [...value.matchAll(LENGTH)].some(
        ([length]) => Number.parseFloat(length) !== 0,
    );
}

function literalColour(value) {
    return HEX.test(value) || COLOUR_FUNCTION.test(value);
}

function styleSpacing(value) {
    if (value?.type !== "Literal") return false;
    return typeof value.value === "number"
        ? value.value !== 0
        : typeof value.value === "string" && literalLength(value.value);
}

const designTokens = {
    meta: {
        type: "problem",
        messages: {
            colour: "Use a colour token from src/styles/tokens instead of a literal colour.",
            spacing:
                "Use a spacing token (var(--space-*)) instead of a literal length.",
        },
        schema: [],
    },
    create(context) {
        const source = context.sourceCode;
        const literal = (node, text) => {
            if (typeof text === "string" && literalColour(text))
                context.report({ node, messageId: "colour" });
        };
        return {
            Declaration(node) {
                const declaration = source.getText(node);
                const value = declaration.slice(declaration.indexOf(":") + 1);
                const property = node.property.toLowerCase();
                if (
                    literalColour(value) ||
                    (COLOUR_PROPERTY.test(property) && NAMED_COLOUR.test(value))
                )
                    context.report({ node, messageId: "colour" });
                else if (SPACING.test(property) && literalLength(value))
                    context.report({ node, messageId: "spacing" });
            },
            Literal(node) {
                literal(node, node.value);
            },
            TemplateElement(node) {
                literal(node, node.value.cooked);
            },
            "JSXAttribute[name.name='style'] > JSXExpressionContainer > ObjectExpression > Property"(
                node,
            ) {
                const key =
                    node.key.type === "Identifier"
                        ? node.key.name
                        : String(node.key.value);
                if (STYLE_SPACING.test(key) && styleSpacing(node.value))
                    context.report({ node, messageId: "spacing" });
            },
        };
    },
};

const BRAND_PROPERTY = /^--(?:forest|amber|cream|teal)(?:-|$)/;

const brandSource = {
    meta: {
        type: "problem",
        messages: {
            brand: "Declare a brand colour only in src/styles/tokens.ts.",
        },
        schema: [],
    },
    create(context) {
        const brandName = (name) =>
            typeof name === "string" && BRAND_PROPERTY.test(name);
        return {
            Declaration(node) {
                if (brandName(node.property))
                    context.report({ node, messageId: "brand" });
            },
            Property(node) {
                if (node.key.type === "Literal" && brandName(node.key.value))
                    context.report({ node, messageId: "brand" });
            },
        };
    },
};

export default {
    meta: { name: "pyracast" },
    rules: {
        "design-tokens": designTokens,
        "brand-source": brandSource,
        "browser-clock": browserClock,
    },
};
