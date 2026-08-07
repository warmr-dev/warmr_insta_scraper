"""SPEC section 11 - explicit do-nots, asserted at the source level.

These are not behavioural tests: they are structural guards. Each one encodes a
rule whose violation burns Instagram accounts or leaks credentials, and each is
checked with the `ast` module rather than grep so that comments, docstrings and
string literals cannot produce a false positive (or, worse, hide a real one).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
SCRIPTS = REPO_ROOT / "scripts"

# The single module SPEC section 11 permits to import instagrapi.
TRANSPORT_LIVE = SRC / "stories_monitor" / "transport" / "live.py"


def _python_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    if SCRIPTS.is_dir():
        files += sorted(SCRIPTS.rglob("*.py"))
    assert files, "no source files found - the invariant checks would vacuously pass"
    return files


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# --- instagrapi is confined to the transport layer ---------------------------


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module named by a real `import` / `from ... import` statement.

    AST-based on purpose: a mention of instagrapi in a comment or docstring is
    documentation, not a dependency, and must not fail this test.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import can never be instagrapi
                continue
            if node.module:
                modules.add(node.module)
    return modules


def _top_level(module: str) -> str:
    return module.split(".", 1)[0]


def test_instagrapi_is_imported_only_in_transport_live():
    """SPEC section 11: do not import instagrapi outside the transport layer."""
    offenders: list[str] = []
    for path in _python_files():
        if path == TRANSPORT_LIVE:
            continue
        modules = _imported_modules(_parse(path))
        if any(_top_level(m) == "instagrapi" for m in modules):
            offenders.append(_rel(path))

    assert offenders == [], (
        "instagrapi may only be imported in transport/live.py; found imports in: "
        f"{offenders}"
    )


def test_transport_live_really_is_the_module_that_imports_instagrapi():
    """Guard against the previous test passing vacuously (e.g. after a rename)."""
    modules = _imported_modules(_parse(TRANSPORT_LIVE))
    assert any(_top_level(m) == "instagrapi" for m in modules), (
        f"{_rel(TRANSPORT_LIVE)} no longer imports instagrapi - has the live "
        "transport moved? The confinement test above would now pass vacuously."
    )


def test_no_module_imports_instagrapi_at_package_import_time():
    """Fixture mode must run without instagrapi installed (SPEC section 4)."""
    transport_init = SRC / "stories_monitor" / "transport" / "__init__.py"
    modules = _imported_modules(_parse(transport_init))
    assert not any(_top_level(m) == "instagrapi" for m in modules)

    # `from .live import LiveTransport` must be lazy - never at module scope.
    tree = _parse(transport_init)
    for node in tree.body:  # module-scope statements only
        if isinstance(node, ast.ImportFrom) and node.module == "live":
            pytest.fail(
                "transport/__init__.py imports .live at module scope; fixture mode "
                "would then require instagrapi to be installed"
            )


# --- no write endpoints against target accounts ------------------------------

# SPEC section 11: marking stories seen puts our workers in the target's viewer
# list, which gets us reported. Read-only always.
FORBIDDEN_CALL_NAMES = {
    "media_seen",
    "story_seen",
    "mark_seen",
    "seen_reels",
    "reels_seen",
}
FORBIDDEN_ENDPOINT_FRAGMENTS = (
    "media/seen",
    "story_seen",
    "stories/seen",
    "reels/seen",
)


def _called_names(tree: ast.Module) -> set[str]:
    """Names of every call target: `f()` -> 'f', `obj.m()` -> 'm'."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_no_call_to_media_seen_or_mark_seen_anywhere():
    """SPEC 7.1 / section 11: never call `media/seen/` or any write endpoint."""
    offenders: list[str] = []
    for path in _python_files():
        called = _called_names(_parse(path))
        hits = called & FORBIDDEN_CALL_NAMES
        if hits:
            offenders.append(f"{_rel(path)}: {sorted(hits)}")

    assert offenders == [], (
        "a seen-marking call would put our worker accounts into the target's "
        f"viewer list (SPEC section 11): {offenders}"
    )


def test_no_seen_endpoint_string_is_ever_requested():
    """The endpoint path itself must not appear as a request argument.

    Only string literals that are *used* (assigned, or passed to a call) count -
    a path quoted inside a docstring explaining the prohibition is fine.
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = _parse(path)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            lowered = node.value.lower()
            if any(fragment in lowered for fragment in FORBIDDEN_ENDPOINT_FRAGMENTS):
                offenders.append(f"{_rel(path)}:{node.lineno}: {node.value!r}")

    assert offenders == [], f"a seen endpoint is referenced in code: {offenders}"


# --- challenges are never auto-solved ----------------------------------------


def test_live_transport_challenge_resolve_raises_rather_than_solving():
    """SPEC 7.8: instagrapi auto-solves challenges from inside private_request.

    The override must raise. Asserted structurally so the guard cannot be
    silently softened into a `return False`.
    """
    tree = _parse(TRANSPORT_LIVE)

    override = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "challenge_resolve":
            override = node
            break

    assert override is not None, (
        "transport/live.py no longer overrides challenge_resolve; instagrapi "
        "would auto-solve challenges from inside private_request (SPEC 7.8)"
    )

    raises = [n for n in ast.walk(override) if isinstance(n, ast.Raise)]
    assert raises, "challenge_resolve must raise, not solve"

    returns = [
        n for n in ast.walk(override) if isinstance(n, ast.Return) and n.value is not None
    ]
    assert not returns, (
        "challenge_resolve returns a value - it must only ever raise (SPEC 7.8)"
    )


def test_challenge_resolve_raises_at_runtime_too():
    pytest.importorskip("instagrapi", reason="instagrapi is not installed")
    from stories_monitor.transport.base import ChallengeRequiredError
    from stories_monitor.transport.live import _NoChallengeClient

    client = _NoChallengeClient.__new__(_NoChallengeClient)
    with pytest.raises(ChallengeRequiredError):
        client.challenge_resolve({"challenge": {"api_path": "/challenge/"}})


def test_no_challenge_solving_helpers_are_defined_or_called():
    """No code path may enter a verification code, SMS or email challenge flow."""
    forbidden = {
        "challenge_code",
        "challenge_send_security_code",
        "challenge_resolve_simple",
        "send_security_code",
        "submit_challenge_code",
        "solve_challenge",
    }
    offenders: list[str] = []
    for path in _python_files():
        tree = _parse(path)
        defined = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        hits = (defined | _called_names(tree)) & forbidden
        if hits:
            offenders.append(f"{_rel(path)}: {sorted(hits)}")

    assert offenders == [], f"challenge-solving code found (SPEC 7.8): {offenders}"


# --- no hardcoded credentials ------------------------------------------------

SECRET_NAME_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "proxy_url",
)

# Names that look secret but are legitimately non-secret values.
ALLOWED_NAMES = {
    "password_enc",       # a column name / encrypted blob field
    "secret_name_fragments",
    "redact_keys",
}

# Values that are obviously not credentials.
ALLOWED_VALUE_PREFIXES = (
    "replace",      # REPLACE_ME, REPLACE_WITH_FERNET_KEY
    "your_",
    "changeme",
    "placeholder",
    "todo",
    "example",
    "dummy",
    "fake",
    "test",
    "xxx",
    "<",
    "$",
    "***",
)
ALLOWED_VALUES = {
    "",
    "none",
    "null",
    "password",
    "token",
    "secret",
    "api_key",
    # env var names and dict keys, not values
    "secret_key",
    "anthropic_api_key",
    "slack_bot_token",
    "proxy_url",
    "sessionid",
    "csrftoken",
    "authorization",
    "cookie",
    "cookies",
    "ds_user_id",
    "session_json",
}


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    if lowered in ALLOWED_NAMES:
        return False
    return any(fragment in lowered for fragment in SECRET_NAME_FRAGMENTS)


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ALLOWED_VALUES:
        return True
    return any(lowered.startswith(prefix) for prefix in ALLOWED_VALUE_PREFIXES)


def _assigned_targets(node: ast.AST) -> list[str]:
    """Names/attributes/keywords a literal is being bound to."""
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Attribute):
                names.append(target.attr)
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node.target, ast.Attribute):
            names.append(node.target.attr)
    return names


def test_no_hardcoded_credentials_in_src():
    """SPEC section 11: never hardcode credentials, proxies or API keys.

    Flags any assignment of a non-empty string literal to a password/token/
    api_key/proxy-shaped name, including keyword arguments and dict literals.
    """
    offenders: list[str] = []

    for path in _python_files():
        tree = _parse(path)

        for node in ast.walk(tree):
            # name = "literal"  /  self.name = "literal"  /  name: T = "literal"
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                for name in _assigned_targets(node):
                    if _looks_secret(name) and not _is_placeholder(value.value):
                        offenders.append(
                            f"{_rel(path)}:{node.lineno}: {name} = {value.value!r}"
                        )

            # f(password="literal")
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg is None:
                        continue
                    if not isinstance(keyword.value, ast.Constant):
                        continue
                    if not isinstance(keyword.value.value, str):
                        continue
                    if _looks_secret(keyword.arg) and not _is_placeholder(
                        keyword.value.value
                    ):
                        offenders.append(
                            f"{_rel(path)}:{node.lineno}: "
                            f"{keyword.arg}={keyword.value.value!r}"
                        )

            # {"password": "literal"}
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    if not isinstance(value, ast.Constant) or not isinstance(
                        value.value, str
                    ):
                        continue
                    if _looks_secret(key.value) and not _is_placeholder(value.value):
                        offenders.append(
                            f"{_rel(path)}:{node.lineno}: "
                            f"{key.value!r}: {value.value!r}"
                        )

    assert offenders == [], f"hardcoded credentials found (SPEC section 11): {offenders}"


def test_the_credential_scanner_actually_catches_a_hardcoded_secret(tmp_path):
    """Guard against the scanner above silently matching nothing."""
    sample = tmp_path / "bad.py"
    sample.write_text(
        'password = "hunter2"\n'
        'client = login(api_key="sk-ant-real")\n'
        'cfg = {"slack_bot_token": "xoxb-real"}\n',
        encoding="utf-8",
    )
    tree = _parse(sample)

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for name in _assigned_targets(node):
                if _looks_secret(name) and isinstance(node.value, ast.Constant):
                    if not _is_placeholder(node.value.value):
                        found.append(name)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg
                    and _looks_secret(keyword.arg)
                    and isinstance(keyword.value, ast.Constant)
                    and not _is_placeholder(keyword.value.value)
                ):
                    found.append(keyword.arg)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and _looks_secret(str(key.value))
                    and isinstance(value, ast.Constant)
                    and not _is_placeholder(str(value.value))
                ):
                    found.append(str(key.value))

    assert set(found) == {"password", "api_key", "slack_bot_token"}


def test_secrets_come_from_the_environment_only():
    """Config defaults must be placeholders, never real values (SPEC section 4)."""
    from stories_monitor.config import Settings

    fields = Settings.model_fields
    for name in ("anthropic_api_key", "slack_bot_token"):
        assert fields[name].default == "", f"{name} must default to empty, not a value"
    assert fields["secret_key"].default == "REPLACE_WITH_FERNET_KEY"


def test_env_example_contains_only_placeholders():
    """`.env.example` ships with the repo - it must never carry a real secret."""
    example = REPO_ROOT / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example is not present")

    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if _looks_secret(key) and value.strip():
            assert _is_placeholder(value), (
                f".env.example may contain a real credential: {key}={value!r}"
            )


def test_dotenv_is_gitignored():
    """SPEC section 4: never commit a `.env`."""
    gitignore = REPO_ROOT / ".gitignore"
    if not gitignore.is_file():
        pytest.skip("no .gitignore")
    patterns = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert patterns & {".env", "*.env", ".env*"}, ".env is not gitignored"


# --- other SPEC section 11 do-nots -------------------------------------------


def test_no_distributed_task_framework_is_imported():
    """SPEC section 3 / 11: Redis lists plus small worker loops, nothing heavier."""
    forbidden = {"celery", "airflow", "django", "dramatiq", "rq", "prefect", "luigi"}
    offenders: list[str] = []
    for path in _python_files():
        hits = {_top_level(m) for m in _imported_modules(_parse(path))} & forbidden
        if hits:
            offenders.append(f"{_rel(path)}: {sorted(hits)}")
    assert offenders == [], f"a distributed task framework was added: {offenders}"


def test_no_loop_over_per_target_story_calls():
    """SPEC section 11: do not poll targets individually.

    `user_stories(user_id)` in a loop over 57,000 targets is the design mistake
    the whole reels_tray architecture exists to avoid.
    """
    forbidden = {"user_stories", "user_stories_v1", "user_story", "story_info"}
    offenders: list[str] = []
    for path in _python_files():
        hits = _called_names(_parse(path)) & forbidden
        if hits:
            offenders.append(f"{_rel(path)}: {sorted(hits)}")
    assert offenders == [], (
        f"per-target story polling found - re-read SPEC section 1: {offenders}"
    )


def test_media_is_never_stored_beyond_the_analysis_step():
    """SPEC section 11: the analyzer deletes the temp file in a `finally`."""
    analyzer = SRC / "stories_monitor" / "workers" / "analyzer.py"
    tree = _parse(analyzer)

    process_one = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "process_one"
        ),
        None,
    )
    assert process_one is not None

    tries = [n for n in ast.walk(process_one) if isinstance(n, ast.Try)]
    assert tries, "process_one has no try/finally - media could leak on an error path"

    cleanup_in_finally = False
    for node in tries:
        for stmt in node.finalbody:
            for call in ast.walk(stmt):
                if isinstance(call, ast.Call):
                    func = call.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                    if "delete" in name or "unlink" in name or "cleanup" in name:
                        cleanup_in_finally = True

    assert cleanup_in_finally, (
        "the temp media file is not deleted in a `finally` block (SPEC 7.4 step 4)"
    )
