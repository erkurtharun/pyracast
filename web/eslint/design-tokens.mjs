// Colours and spacing come only from src/styles/tokens.{css,ts}; the config switches this rule off there.
const HEX = /(?:^|[\s(,:])#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![\w-])/i;
const SPACING =
    /^(?:margin|padding|gap|row-gap|column-gap|inset|scroll-margin|scroll-padding)(?:-|$)/;
const LENGTH = /(?<![\w.-])-?(?:\d*\.\d+|\d+)(?:px|rem|em)\b/g;

function literalLength(value) {
    return [...value.matchAll(LENGTH)].some(
        ([length]) => Number.parseFloat(length) !== 0,
    );
}

const designTokens = {
    meta: {
        type: "problem",
        messages: {
            colour: "Use a colour token from src/styles/tokens instead of a hex value.",
            spacing:
                "Use a spacing token (var(--space-*)) instead of a literal length.",
        },
        schema: [],
    },
    create(context) {
        const source = context.sourceCode;
        const literal = (node, text) => {
            if (typeof text === "string" && HEX.test(text))
                context.report({ node, messageId: "colour" });
        };
        return {
            Declaration(node) {
                const declaration = source.getText(node);
                const value = declaration.slice(declaration.indexOf(":") + 1);
                if (HEX.test(value))
                    context.report({ node, messageId: "colour" });
                else if (
                    SPACING.test(node.property.toLowerCase()) &&
                    literalLength(value)
                )
                    context.report({ node, messageId: "spacing" });
            },
            Literal(node) {
                literal(node, node.value);
            },
            TemplateElement(node) {
                literal(node, node.value.cooked);
            },
        };
    },
};

export default {
    meta: { name: "pyracast" },
    rules: { "design-tokens": designTokens },
};
