from .._grammar import select
from .._guidance import guidance
from ._optional import optional
from ._sequences import zero_or_more
from ._subgrammar import lexeme, subgrammar


class Tool:
    def __init__(self, call_grammar=None, tool_call=None, callable=None):
        # call_grammar specifies how the tool can be called. Crucially, it has to capture the args in variable 'tool_args'
        # tool_call is a guidance function  actually calls the tool, and returns an lm object with whatever outputs it wants
        # callable: guidance function or regular callable, will be converted to grammar
        # TODO: hidden is not working yet
        first_option = (call_grammar is not None) and (tool_call is not None)
        second_option = callable is not None
        # either both are true or both false
        if first_option == second_option:
            raise Exception("Must pass either (call_grammar, tool call) or callable, but not both or neither")
        if second_option:
            call_grammar, tool_call = fn_to_grammar_call(callable)
        self.call_grammar = call_grammar
        self.tool_call = tool_call


def basic_func_grammar(name):
    arg = lexeme(r"[^,=)]+")
    kwarg = arg + "=" + arg
    args = arg + zero_or_more("," + arg)
    kwargs = kwarg + zero_or_more("," + kwarg)

    obj = name + "("
    obj += subgrammar(
        name="tool_args",
        body=optional(
            select(
                [
                    args,
                    kwargs,
                    args + "," + kwargs,
                ]
            )
        ),
        skip_regex=r" *",
    )
    obj += ")"
    return obj


def fn_to_grammar_call(callable):
    # TODO later: validate the call. Here is code to get required and optional args of 'guidance_fn':
    # name = guidance_fn.__name__
    # required_args = []
    # optional_args = []
    # sig = inspect.signature(guidance_fn)
    # for i, x in enumerate(sig.parameters.values()):
    #     if i == 0:
    #         continue
    #     if x.default is x.empty:
    #         required_args.append(x.name)
    #     else:
    #         optional_args.append(x.name)
    name = callable.__name__
    call_grammar = basic_func_grammar(name)

    @guidance(dedent=False)
    def basic_tool_call(lm):
        args = lm["tool_args"]
        args = args.split(",")
        positional = [x.strip() for x in args if "=" not in x]
        kwargs = dict([tuple(x.strip().split("=")) for x in args if "=" in x])
        lm += callable(*positional, **kwargs)
        return lm

    return call_grammar, basic_tool_call
