import pytest

from blender_lab_mcp_client.server import _COMMAND_CODE, _build_code, _build_header

AWKWARD = [
    "plain",
    "it's",
    'say "hi"',
    "both ' and \"",
    "'''triple single'''",
    '"""triple double"""',
    "back\\slash",
    "trailing backslash\\",
    "\\n is not a newline",
    "line1\nline2\r\nline3\ttab",
    "nul\x00byte",
    "\u2028 line sep \u2029 para sep",
    "héllo wörld — ✓",
    "日本語のパス",
    "astral 😀 𝔘",
    "C:\\Users\\rober\\My Documents\\file.blend",
    'C:\\Users\\x\\"quoted"\\out.png',
    "\\\\server\\share\\x.png",
    "//relative/blender/path.png",
    "{} {0} %s %(x)s {name}",
    "'; import os; os.system('x') #",
    '"""\nimport os\n"""',
    "",
]


@pytest.mark.parametrize("value", AWKWARD)
def test_header_round_trips_string(stub_bpy, value):
    ns: dict = {}
    exec(_build_header({"filepath": value}), ns)
    assert ns["params"] == {"filepath": value}


def test_header_round_trips_nested_structures(stub_bpy):
    params = {
        "name": AWKWARD[3],
        "location": [1.5, -2.0, 0],
        "args": {"k": ["a'b", {"deep": '"""'}], "none": None, "t": True},
        "path": AWKWARD[15],
        AWKWARD[4]: "weird key",
    }
    ns: dict = {}
    exec(_build_header(params), ns)
    assert ns["params"] == params


def test_header_has_no_user_text_outside_a_string_literal(stub_bpy):
    ns: dict = {"marker": "unset"}
    exec(_build_header({"x": "'\nmarker = 'pwned'\n'"}), ns)
    assert ns["marker"] == "unset"


@pytest.mark.parametrize("value", AWKWARD)
def test_generated_code_is_ascii_and_has_single_params_line(value):
    code = _build_code("object.delete", {"name": value})
    assert code.isascii()
    assert sum(1 for line in code.splitlines() if line.startswith("params = ")) == 1


@pytest.mark.parametrize("command", sorted(_COMMAND_CODE))
def test_every_snippet_compiles(command):
    compile(_build_code(command, {}), command, "exec")


def test_unsupported_command():
    with pytest.raises(RuntimeError, match="Unsupported command"):
        _build_code("nope.nothing", {})
