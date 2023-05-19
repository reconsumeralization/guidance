import asyncio
import re
import uuid
import logging
from .._grammar import grammar
from .._utils import escape_template_block

log = logging.getLogger(__name__)

async def gen(variable_name="generated", stop=None, stop_regex=None, max_tokens=500, n=1, temperature=0.0, top_p=1.0,
              logprobs=None, pattern=None, hidden=False, parse=False, list_append=False, save_prompt=False,
              token_healing=None, _parser_context=None):
    ''' Use the LLM to generate a completion.

    Parameters
    ----------
    variable_name : str
        The name of the variable to store the generated value in.
    stop : str
        The stop string to use for stopping generation. If not provided, the next node's text will be used if
        that text matches a closing quote, XML tag, or role end. Note that the stop string is not included in
        the generated value.
    stop_regex : str
        A regular expression to use for stopping generation. If not provided, the stop string will be used.
    max_tokens : int
        The maximum number of tokens to generate in this completion.
    n : int
        The number of completions to generate. If you generate more than one completion, the variable will be
        set to a list of generated values. Only the first completion will be used for future context for the LLM,
        but you may often want to use hidden=True when using n > 1.
    temperature : float
        The temperature to use for generation. A higher temperature will result in more random completions. Note
        that caching is always on for temperature=0, and is seed-based for other temperatures.
    top_p : float
        The top_p value to use for generation. A higher top_p will result in more random completions.
    logprobs : int or None
        If set to an integer, the LLM will return that number of top log probabilities for the generated tokens
        which will be stored in a variable named `variable_name+"_logprobs"`. If set to None, the log
        probabilities will not be returned.
    pattern : str or None
        A regular expression pattern guide to use for generation. If set the LLM will be forced (through guided
        decoding) to only generate completions that match the regular expression.
    hidden : bool
        Whether to hide the generated value from future LLM context. This is useful for generating completions
        that you just want to save in a variable and not use for future context.
    parse : bool
        Whether to parse the generated value. If set to True, the generated value will be parsed as a guidance
        program. Warning: This has not been tested for security yet, and needs to controls added to be safe. If
        you need to use this, consider a PR to add the necessary security controls.
    list_append : bool
        Whether to append the generated value to a list stored in the variable. If set to True, the variable
        must be a list, and the generated value will be appended to the list.
    save_prompt : str or bool
        If set to a string, the exact prompt given to the LLM will be saved in a variable with the given name.
    token_healing : bool or None
        If set to a bool this overrides the token_healing setting for the LLM.
    '''
    prefix = ""
    suffix = ""

    # get the parser context variables we will need
    parser = _parser_context['parser']
    next_node = _parser_context["next_node"]
    parser_prefix = _parser_context["parser_prefix"]
    partial_output = _parser_context["partial_output"]

    # if stop is None then we use the text of the node after the generate command
    if stop is None:

        next_text = next_node.text if next_node is not None else ""
        prev_node = _parser_context["prev_node"]
        prev_text = prev_node.text if prev_node is not None else ""
        next_next_node = _parser_context["next_next_node"]
        if next_next_node and next_next_node.text.startswith("{{~"):
            next_text = next_text.lstrip()
            if next_next_node and next_text == "":
                next_text = next_next_node.text

        # auto-detect quote stop tokens
        quote_types = ["'''", '"""', '```', '"', "'", "`"]
        for quote_type in quote_types:
            if next_text.startswith(quote_type) and prev_text.endswith(quote_type):
                stop = quote_type
                break

        # auto-detect role stop tags
    if stop is None:
        if m := re.match(
            r"^{{~?/(user|assistant|system|role)~?}}.*", next_text
        ):
            stop = parser.program.llm.role_end(m[1])

        # auto-detect XML tag stop tokens
    if stop is None:
        m = re.match(r"<([^>\W]+)[^>]+>", next_text)
        if m is not None:
            end_tag = f"</{m[1]}>"
            if next_text.startswith(end_tag):
                stop = end_tag

    # fall back to the next node's text
    if stop is None:
        stop = next_text



    if stop == "":
        stop = None

    # set the cache seed to 0 if temperature is 0
    if temperature > 0:
        cache_seed = parser.program.cache_seed
        parser.program.cache_seed += 1
    else:
        cache_seed = 0

    # see if we should stream the results
    if n == 1: # we can't stream batches right now
        if parser.program.stream == "auto":
            stream_generation = not parser.program.silent or parser.program.async_mode
        else:
            stream_generation = parser.program.stream
    else:
        stream_generation = False

    # save the prompt if requested
    if save_prompt:
        parser.set_variable(save_prompt, parser_prefix+prefix)

    if logprobs is None:
        logprobs = parser.program.logprobs

    # call the LLM
    gen_obj = await parser.llm_session(
        parser_prefix+prefix, stop=stop, stop_regex=stop_regex, max_tokens=max_tokens, n=n, pattern=pattern,
        temperature=temperature, top_p=top_p, logprobs=logprobs, cache_seed=cache_seed, token_healing=token_healing,
        echo=parser.program.logprobs is not None, stream=stream_generation, caching=parser.program.caching
    )

    if n == 1:
        generated_value = prefix
        partial_output(generated_value)
        logprobs_out = []
        if not stream_generation:
            gen_obj = [gen_obj]
        if list_append:
            value_list = parser.get_variable(variable_name, [])
            value_list.append("")
            if logprobs is not None:
                logprobs_list = parser.get_variable(f"{variable_name}_logprobs", [])
                logprobs_list.append([])
        for resp in gen_obj:
            await asyncio.sleep(0) # allow other tasks to run
            #log("parser.should_stop = " + str(parser.should_stop))
            if parser.should_stop:
                #log("Stopping generation")
                break
            # log.debug("resp", resp)
            generated_value += resp["choices"][0]["text"]
            partial_output(resp["choices"][0]["text"])
            if logprobs is not None:
                logprobs_out.extend(resp["choices"][0]["logprobs"])
            if list_append:
                value_list[-1] = generated_value
                parser.set_variable(variable_name, value_list)
                if logprobs is not None:
                    logprobs_list[-1] = logprobs_out
                    parser.set_variable(f"{variable_name}_logprobs", logprobs_list)
            else:
                parser.set_variable(variable_name, generated_value)
                if logprobs is not None:
                    parser.set_variable(f"{variable_name}_logprobs", logprobs_out)
        if hasattr(gen_obj, 'close'):
            gen_obj.close()
        generated_value += suffix
        partial_output(suffix)
        if list_append:
            value_list[-1] = generated_value
            parser.set_variable(variable_name, value_list)
        else:
            parser.set_variable(variable_name, generated_value)

        if parse:
            assert not hidden, "Cannot parse generated text if we are hiding the output" # TODO: fix this?
            subtree = grammar.parse(generated_value)
            return await parser.visit(subtree)
        else:
            # stop executing if we were interrupted
            if parser.should_stop:
                parser.executing = False
                parser.should_stop = False
            return
    else:
        assert not isinstance(gen_obj, list), "Streaming is only supported for n=1"
        generated_values = [prefix+choice["text"]+suffix for choice in gen_obj["choices"]]
        if list_append:
            value_list = parser.get_variable(variable_name, [])
            value_list.append(generated_values)
            if logprobs is not None:
                logprobs_list = parser.get_variable(f"{variable_name}_logprobs", [])
                logprobs_list.append([choice["logprobs"] for choice in gen_obj["choices"]])
        else:
            parser.set_variable(variable_name, generated_values)
            if logprobs is not None:
                parser.set_variable(
                    f"{variable_name}_logprobs",
                    [choice["logprobs"] for choice in gen_obj["choices"]],
                )

        if not hidden:
            # TODO: we could enable the parsing to branch into multiple paths here, but for now we just complete the program with the first prefix
            generated_value = generated_values[0]

            # echoing with multiple completions is not standard behavior
            # this just uses the first generated value for completion and the rest as alternatives only used for the variable storage
            # we mostly support this so that the echo=False hiding behavior does not make multiple outputs more complicated than it needs to be in the UX
            # if echo:
            #     partial_output(generated_value) 

            id = uuid.uuid4().hex
            l = len(generated_values)
            out = "{{!--" + f"GMARKERmany_generate_start_{not hidden}_{l}${id}$" + "--}}"
            for i, value in enumerate(generated_values):
                if i > 1:
                    out += "--}}"
                if i > 0:
                    out += "{{!--" + f"GMARKERmany_generate_{not hidden}_{i}${id}$" + "--}}{{!--G "
                    out += escape_template_block(value)
                else:
                    out += value
            partial_output(out + "--}}{{!--" + f"GMARKERmany_generate_end${id}$" + "--}}")
            return
            # return "{{!--GMARKERmany_generate_start$$}}" + "{{!--GMARKERmany_generate$$}}".join([v for v in generated_values]) + "{{!--GMARKERmany_generate_end$$}}"
            # return "".join([v for v in generated_values])