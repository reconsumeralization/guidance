import guidance

def test_gen():
    """ Test that LM geneation works.
    """

    llm = guidance.llms.Mock(" Sue")
    prompt = guidance("Hello my name is{{gen 'name' max_tokens=5}}", llm=llm)
    out = prompt()
    assert len(out["name"]) > 1

def test_gen_n_greater_than_one():
    llm = guidance.llms.Mock(["mock output 0", "mock output 1", "mock output 2"])
    prompt = guidance('''The best thing about the beach is{{gen 'best' n=3 temperature=0.7 max_tokens=5}}''', llm=llm)
    a = prompt()
    assert "\n".join(a["best"]) == 'mock output 0\nmock output 1\nmock output 2'

def test_gen_n_greater_than_one_hidden():
    llm = guidance.llms.Mock()

    def aggregate(best):
        return '\n'.join([f'- {x}' for x in best])

    prompt = guidance('''The best thing about the beach is{{gen 'best' temperature=0.7 n=3 hidden=True}}
{{aggregate best}}''', llm=llm)
    a = prompt(aggregate=aggregate)
    assert str(a) == 'The best thing about the beach is\n- mock output 0\n- mock output 1\n- mock output 2'

def test_pattern():
    import re
    llm = guidance.llms.Transformers("gpt2")
    out = guidance('''On a scale of 1-10 I would say it is: {{gen 'score' pattern="[0-9]+"}}''', llm=llm)()
    assert re.match(r'[0-9]+', out["score"])