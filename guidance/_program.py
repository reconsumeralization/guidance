import ast
import inspect
import re
import html
import uuid
import sys
import parsimonious
import logging
import copy
import asyncio
import pathlib
import os
import traceback
import time
import datetime
from .llms import _openai
from . import _utils
from ._program_executor import ProgramExecutor
from . import library
import guidance
log = logging.getLogger(__name__)

# load the javascript client code
file_path = pathlib.Path(__file__).parent.parent.absolute()
with open(file_path / "guidance" / "resources" / "main.js", encoding="utf-8") as f:
    js_data = f.read()

class Program:
    ''' A program template that can be compiled and executed to generate a new filled in (executed) program.

    Note that as the template gets executed {{!-- handlebars comment markers --}} get left in
    the generated output to mark where template tags used to be.
    '''

    def __init__(self, text, llm=None, cache_seed=0, logprobs=None, stream=False, echo=False, **kwargs):
        """ Create a new Program object from a program string.

        Parameters
        ----------
        text : str
            The program string to use as a guidance template.
        llm : guidance.llms.LLM (defaults to guidance.llm)
            The language model to use for executing the program.
        cache_seed : int (default 0) or None
            The seed to use for the cache. If you want to use the same cache for multiple programs
            you can set this to the same value for all of them. Set this to None to disable caching.
            Caching is enabled by default, and saves calls that have tempurature=0, and also saves
            higher temperature calls but uses different seed for each call.
        logprobs : int or None (default)
            The number of logprobs to return from the language model for each token. (not well supported yet,
            since some endpoints don't support it)
        stream : bool (default False)
            If True, the program will be executed in "streaming" mode, where the program is executed
            asyncronously. If the program is displayed while streaming the output is generated per token.
        echo : bool (default False)
            If True, the program will be displayed as it is executed (this is an alternative to using stream=True
            and then displaying the async object).
        """

        # see if we were given a raw function instead of a string template
        # if so, convert it to a string template that calls the function
        if not isinstance(text, str):
            if callable(text):
                sig = inspect.signature(text)
                args = ""
                for name,_ in sig.parameters.items():
                    args += f" {name}={name}"
                fname = _utils.find_func_name(text, kwargs)
                kwargs[fname] = text
                text = "{{set (%s%s)}}" % (fname, args)
        
        # save the given parameters
        self._text = text
        self.llm = llm or guidance.llm
        self.cache_seed = cache_seed
        self.logprobs = logprobs
        self.stream = stream
        self.echo = echo
        
        # set our variables
        self.variables = {}
        self.variables.update(_built_ins)
        self.variables.update(kwargs)
        
        # set internal state variables
        self._id = str(uuid.uuid4())
        self.display_debounce_delay = 0.1 # the minimum time between display updates
        self._comm = None # front end communication object
        self._executor = None # the ProgramExecutor object that is running the program
        self._last_display_update = 0 # the last time we updated the display (used for throttling updates)
        self._execute_complete = asyncio.Event() # fires when the program is done executing to resolve __await__
        self._displaying = echo # if we are displaying we need to update the display as we execute
        self._displayed = False # marks if we have been displayed in the client yet
        self._displaying_html = False # if we are displaying html (vs. text)

        # see if we are in an ipython environment
        try:
            self._ipython = get_ipython()
        except:
            self._ipython = None
        
        # if we are echoing in ipython we assume we can display html
        if self._ipython and echo:
            self._displaying_html = True

        # get or create an event loop
        if asyncio.get_event_loop().is_running():
            self.event_loop = asyncio.get_event_loop()
        else:
            self.event_loop = asyncio.new_event_loop()
    
    def __repr__(self):
        return self.text
    
    def __getitem__(self, key):
        return self.variables[key]
    
    def _interface_event(self, msg):
        """ Handle an event from the front end.
        """
        if msg["event"] == "stop":
            self._executor.stop()
        elif msg["event"] == "opened":
            pass # we don't need to do anything here because the first time we display we'll send the html
        pass

    def _ipython_display_(self):
        """ Display the program in the ipython notebook.
        """

        log.debug(f"displaying program in _ipython_display_ with self._comm={self._comm}, self.id={self._id}")
        
        # mark that we are displaying (and so future execution updates should be displayed)
        self._displaying = True
        self._displaying_html = True
        
        # build and display the html
        html = self._build_html(self.marked_text)
        self._display_html(html)
        

    async def _await_finish_execute(self):
        """ Used by self.__await__ to wait for the program to complete.
        """
        await self._execute_complete.wait() # wait for the program to finish executing
        return self

    def __await__(self):
        return self._await_finish_execute().__await__()
        
    def __call__(self, **kwargs):
        """ Execute this program with the given variable values and return a new executed/executing program.

        Note that the returned program might not be fully executed if `stream=True`. When streaming you need to
        use the python `await` keyword if you want to ensure the program is finished (note that is different than
        the `await` guidance langauge command, which will cause the program to stop execution at that point).
        """

        kwargs = {**{
            "stream": self.stream,
            "echo": self.echo,
        }, **kwargs}

        log.debug(f"in __call__ with kwargs: {kwargs}")

        # create a new program object that we will execute in-place
        new_program = Program(
            self._text,
            self.llm,
            self.cache_seed,
            self.logprobs,

            # copy the (non-function) variables so that we don't modify the original program during execution
            # TODO: what about functions? should we copy them too?
            **{**{k: v if callable(v) else copy.deepcopy(v) for k,v in self.variables.items()}, **kwargs}
        )

        # create an executor for the new program (this also marks the program as executing)
        new_program._executor = ProgramExecutor(new_program)
        
        # if we are streaming schedule the program in the current event loop
        if new_program.stream:
            loop = asyncio.get_event_loop()
            assert self.event_loop.is_running()
            self.event_loop.create_task(new_program.execute())

        # if we are not streaming, we need to create a new event loop and run the program in it until it is done
        else:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(new_program.execute())
    
        return new_program
    
    def _update_display(self, last=False):
        """ Updates the display with the current marked text after debouncing.

        Parameters
        ----------
        last : bool
            If True, this is the last update and we should clear the send queue and prepare the
            UI for saving etc.
        """

        log.debug(f"Updating display (last={last}, self._displaying={self._displaying}, self._comm={self._comm})")

        # this is always called during execution, and we only want to update the display if we are displaying
        if not self._displaying:
            return
        
        # debounce the display updates
        now = time.time()
        log.debug(now - self._last_display_update)
        if last or (now - self._last_display_update > self.display_debounce_delay):
            if self._displaying_html:
                out = self._build_html(self.marked_text)
                
                # clear the send queue if this is the last update
                if last and self._comm:
                    self._comm.clear_send_queue()
                
                # send an update to the front end client if we have one...
                # TODO: we would like to call `display` for the last update so NB saving works, but see https://github.com/microsoft/vscode-jupyter/issues/13243 
                if self._comm and (not last or self._comm.is_open):
                    log.debug(f"Updating display send message to front end")
                    self._comm.send({"replace": out})
                    if last:
                        self._comm.send({"event": "complete"})
                
                # ...otherwise dump the client to the font end
                else:
                    log.debug(f"Updating display dump to front end")
                    from IPython.display import clear_output, display
                    if self._displayed:
                        clear_output(wait=False) # should use wait=True but that doesn't work in VSCode until after the April 2023 release

                    self._display_html(out)
                self._displayed = True
            
            self._last_display_update = time.time()

    def _display_html(self, html):
        from IPython.display import display

        # create the comm object if we don't have one
        if self._comm is None:
            self._comm = _utils.JupyterComm(self._id, self._ipython, self._interface_event)
        
        # dump the html to the front end
        html = f"""<div id="guidance-stop-button-{self._id}" style="cursor: pointer; margin: 0px; display: none; float: right; padding: 3px; border-radius: 4px 4px 4px 4px; border: 0px solid rgba(127, 127, 127, 1); padding-left: 10px; padding-right: 10px; font-size: 13px; background-color: rgba(127, 127, 127, 0.25);">Stop program</div><div id="guidance-content-{self._id}">{html}</div>
<script type="text/javascript">{js_data}; window._guidanceDisplay("{self._id}");</script>"""
        display({"text/html": html}, display_id=self._id, raw=True, clear=True, include=["text/html"])

    async def execute(self):
        """ Execute the current program.

        Note that as execution progresses the program will be incrementally converted
        from a template into a completed string (with variables stored). At each point
        in this process the current template remains valid.
        """

        log.debug(f"Executing program (self.stream={self.stream}, self.echo={self.echo}, self._displaying_html={self._displaying_html})")
        
        # if we are already displaying html, we need to yeild to the event loop so the jupyter comm can initialize
        if self._displaying_html:
            await asyncio.sleep(0)
        
        # run the program and capture the output
        await self._executor.run()
        self._text = self._executor.prefix

        # delete the executor and so mark the program as not executing
        self._executor = None

        # update the display with the final output
        self._update_display(last=True)

        # fire an event noting that execution is complete (this will release any await calls waiting on the program)
        self._execute_complete.set()
    
    def __getitem__(self, key):
        return self.variables[key]
    
    @property
    def text(self):
        # strip out the markers for the unformatted output
        return _utils.strip_markers(self.marked_text)
    
    @property
    def marked_text(self):
        if self._executor is not None:
            return self._executor.prefix
        else:
            return self._text
    
    def _build_html(self, text, last=False):
        output = text

        def start_generate_or_select(x):
            no_echo = "echo=False" in x.group(1)
            alpha = 1.0 if no_echo else 1.0
            
            # script that toggles the viisibility of the next element
            click_script = 'var e = this.nextElementSibling; if (e.style.display == "inline") { e.style.display = "none"; this.style.borderRight = "1px solid rgba(0, 165, 0, 0.25)"; } else { e.style.display = "inline"; this.style.borderRight = "0px";}'

            if no_echo:
                out = f'''<div style='background-color: rgba(0, 165, 0, 0.25); border-radius: 4px 0px 0px 4px; border: 1px solid rgba(0, 165, 0, 1); padding-left: 3px; padding-right: 3px; user-select: none; color: rgb(0, 165, 0, 1.0); display: inline; font-weight: normal; cursor: pointer' onClick='{click_script}'>no echo</div>'''
                out += "<span style='background-color: rgba(0, 165, 0, 0.25); opacity: {}; display: none;' title='{}'>".format(alpha, x.group(1))
            else:
                out = "<span style='background-color: rgba(0, 165, 0, 0.25); opacity: {}; display: inline;' title='{}'>".format(alpha, x.group(1))
            return out
        
        def start_each(x):
            no_echo = "echo=False" in x.group(1)
            alpha = 0.5 if no_echo else 1.0
            color = "rgba(165, 165, 165, 0.1)" #if "geneach" not in x.group(1) else "rgba(0, 165, 0, 0.1)"
            return "<span style='opacity: {}; display: inline; background-color: {};' title='{}'>".format(alpha, color, x.group(1))
        
        def start_block(x):
            escaped_tag = x.group(1)
            if "hidden=True" in escaped_tag:
                display = "none"
            else:
                display = "inline"
            return f"<span style='background-color: rgba(165, 165, 165, 0.1); display: {display};' title='{escaped_tag}'>"

        display_out = html.escape(output)
        display_out = re.sub(r"(\{\{generate.*?\}\})", r"<span style='background-color: rgba(0, 165, 0, 0.25);'>\1</span>", display_out, flags=re.DOTALL)
        display_out = re.sub(r"(\{\{#select\{\{/select.*?\}\})", r"<span style='background-color: rgba(0, 165, 0, 0.25);'>\1</span>", display_out, flags=re.DOTALL)
        display_out = re.sub(r"(\{\{#each [^'\"].*?\{\{/each.*?\}\})", r"<span style='background-color: rgba(0, 138.56128016, 250.76166089, 0.25);'>\1</span>", display_out, flags=re.DOTALL)
        display_out = re.sub(r"(\{\{(?!\!)(?!generate)(?!#select)(?!#each)(?!/each)(?!/select).*?\}\})", r"<span style='background-color: rgba(0, 138.56128016, 250.76166089, 0.25);'>\1</span>", display_out, flags=re.DOTALL)
                

        # format the generate command results
        display_out = re.sub(r"{{!--GMARKER_START_gen\$([^\$]*)\$--}}", start_generate_or_select, display_out)
        display_out = display_out.replace("{{!--GMARKER_END_gen$$--}}", "</span>")
        def click_loop_start(id, total_count, echo, color):
            click_script = '''
function cycle_IDVAL(button_el) {
    var i = 0;
    while (i < 50) {
        var el = document.getElementById("IDVAL_" + i);
        if (el.style.display == "inline") {
            el.style.display = "none";
            var next_el = document.getElementById("IDVAL_" + (i+1));
            if (!next_el) {
                next_el = document.getElementById("IDVAL_0");
            }
            if (next_el) {
                next_el.style.display = "inline";
            }
            break;
        }
        i += 1;
    }
    button_el.innerHTML = (((i+1) % TOTALCOUNT) + 1)  + "/" + TOTALCOUNT;
}
cycle_IDVAL(this);'''.replace("IDVAL", id).replace("TOTALCOUNT", str(total_count)).replace("\n", "")
            out = f'''<div style='background: rgba(255, 255, 255, 0.0); border-radius: 4px 0px 0px 4px; border: 1px solid {color}; border-right: 0px; padding-left: 3px; padding-right: 3px; user-select: none; color: {color}; display: inline; font-weight: normal; cursor: pointer' onClick='{click_script}'>1/{total_count}</div>'''
            out += f"<div style='display: inline;' id='{id}_0'>"
            return out
        def click_loop_mid(id, index, echo):
            alpha = 1.0 if not echo else 0.5
            out = f"</div><div style='display: none; opacity: {alpha}' id='{id}_{index}'>"
            return out
        display_out = re.sub(
            r"{{!--GMARKERmany_generate_start_([^_]+)_([0-9]+)\$([^\$]*)\$--}}",
            lambda x: click_loop_start(x.group(3), int(x.group(2)), x.group(1) == "True", "rgba(0, 165, 0, 0.25)"),
            display_out
        )
        display_out = re.sub(
            r"{{!--GMARKERmany_generate_([^_]+)_([0-9]+)\$([^\$]*)\$--}}",
            lambda x: click_loop_mid(x.group(3), int(x.group(2)), x.group(1) == "True"),
            display_out
        )
        display_out = re.sub(r"{{!--GMARKERmany_generate_end\$([^\$]*)\$--}}", "</div>", display_out)

        # format the each command results
        display_out = re.sub(r"{{!--GMARKER_START_each\$([^\$]*)\$--}}", start_each, display_out)
        display_out = re.sub(
            r"{{!--GMARKER_each_noecho_start_([^_]+)_([0-9]+)\$([^\$]*)\$--}}",
            lambda x: click_loop_start(x.group(3), int(x.group(2)), False, "rgb(100, 100, 100, 1)"),
            display_out
        )
        display_out = re.sub(
            r"{{!--GMARKER_each_noecho_([^_]+)_([0-9]+)\$([^\$]*)\$--}}",
            lambda x: click_loop_mid(x.group(3), int(x.group(2)), False),
            display_out
        )
        display_out = re.sub(r"{{!--GMARKER_each_noecho_end\$([^\$]*)\$--}}", "</div>", display_out)

        # format the geneach command results
        display_out = re.sub(r"{{!--GMARKER_START_geneach\$([^\$]*)\$--}}", start_each, display_out)
        
        # format the set command results
        display_out = re.sub(r"{{!--GMARKER_set\$([^\$]*)\$--}}", r"<div style='background-color: rgba(165, 165, 165, 0); border-radius: 4px 4px 4px 4px; border: 1px solid rgba(165, 165, 165, 1); border-left: 2px solid rgba(165, 165, 165, 1); border-right: 2px solid rgba(165, 165, 165, 1); padding-left: 0px; padding-right: 3px; color: rgb(165, 165, 165, 1.0); display: inline; font-weight: normal; overflow: hidden;'><div style='display: inline; background: rgba(165, 165, 165, 1); padding-right: 5px; padding-left: 4px; margin-right: 3px; color: #fff'>set</div>\1</div>", display_out)
        display_out = re.sub(r"{{!--GMARKER_START_set\$([^\$]*)\$--}}", r"<span style='display: inline;' title='\1'>", display_out)

        display_out = re.sub(r"{{!--GMARKER_START_select\$([^\$]*)\$--}}", start_generate_or_select, display_out)
        display_out = display_out.replace("{{!--GMARKER_END_select$$--}}", "</span>")
        display_out = re.sub(r"{{!--GMARKER_START_variable_ref\$([^\$]*)\$--}}", r"<span style='background-color: rgba(0, 138.56128016, 250.76166089, 0.25); display: inline;' title='\1'>", display_out)
        display_out = display_out.replace("{{!--GMARKER_END_variable_ref$$--}}", "</span>")
        display_out = display_out.replace("{{!--GMARKER_each$$--}}", "")#<div style='border-left: 1px dashed rgb(0, 0, 0, .2); border-top: 0px solid rgb(0, 0, 0, .2); margin-right: -4px; display: inline; width: 4px; height: 24px;'></div>")
        display_out = re.sub(r"{{!--GMARKER_START_block\$([^\$]*)\$--}}", start_block, display_out)
        display_out = re.sub(r"{{!--GMARKER_START_([^\$]*)\$([^\$]*)\$--}}", r"<span style='background-color: rgba(0, 138.56128016, 250.76166089, 0.25); display: inline;' title='\2'>", display_out)
        display_out = re.sub(r"{{!--GMARKER_END_([^\$]*)\$\$--}}", "</span>", display_out)
        
        # strip out comments
        display_out = re.sub(r"{{~?!.*?}}", "", display_out)

        display_out = add_spaces(display_out)
        display_out = "<pre style='margin: 0px; padding: 0px; padding-left: 8px; margin-left: -8px; border-radius: 0px; border-left: 1px solid rgba(127, 127, 127, 0.2); white-space: pre-wrap; font-family: ColfaxAI, Arial; font-size: 15px; line-height: 23px;'>"+display_out+"</pre>"

        return display_out

def add_spaces(s):
    """ This adds spaces so the browser will show leading and trailing newlines.
    """
    if s.startswith("\n"):
        s = " " + s
    if s.endswith("\n"):
        s = s + " "
    return s

_built_ins = {
    "gen": library.gen,
    "each": library.each,
    "geneach": library.geneach,
    "select": library.select,
    "if": library.if_,
    "unless": library.unless,
    "add": library.add,
    "subtract": library.subtract,
    "strip": library.strip,
    "block": library.block,
    "set": library.set,
    "await": library.await_
}