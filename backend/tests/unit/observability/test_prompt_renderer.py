from types import SimpleNamespace

from app.observability.prompt.renderer import render


def tpl(system="S", user="U {{q}}", schema=None):
    return SimpleNamespace(system_prompt=system, user_prompt_template=user,
                           variables_schema=schema or {"type": "object", "required": ["q"],
                                                       "properties": {"q": {"type": "string"}}})


def test_render_success_and_stable_hash():
    r1 = render(tpl(), {"q": "hi"})
    r2 = render(tpl(), {"q": "hi"})
    assert r1.status == "ok"
    assert [m["role"] for m in r1.messages] == ["system", "user"]
    assert r1.messages[1]["content"] == "U hi"
    assert r1.prompt_hash == r2.prompt_hash


def test_missing_variable_is_schema_error_not_empty_string():
    r = render(tpl(), {})
    assert r.status == "schema_error"
    assert r.messages == []


def test_unexpected_placeholder_is_render_error():
    r = render(tpl(user="U {{other}}"), {"q": "hi"})
    assert r.status == "render_error"


def test_hash_changes_when_variables_change():
    assert render(tpl(), {"q": "a"}).prompt_hash != render(tpl(), {"q": "b"}).prompt_hash
