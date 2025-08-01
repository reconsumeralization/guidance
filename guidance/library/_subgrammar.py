from .._ast import GrammarNode, RuleNode
from .._grammar import regex, subgrammar

__all__ = ["as_regular_grammar", "lexeme", "regex", "subgrammar"]


def as_regular_grammar(node: GrammarNode, lexeme=False):
    # TODO: Remove this assertion-only check?
    if isinstance(node, RuleNode):
        rule = node
    else:
        rule = RuleNode("dummy", node)
    assert rule.is_allowed_in_lark_terminal
    return node


def lexeme(body_regex: str, json_string: bool = False):
    if json_string:
        raise NotImplementedError("JSON strings are not supported")
    return regex(body_regex)
