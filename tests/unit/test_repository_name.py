"""
Unit tests for the `RepositoryName` value object and the OCI naming grammar.

These tests are the executable statement of which names this registry accepts.
The grammar is not ours to relax: a name this module accepts but `docker pull`
rejects is a repository no client can ever address, and a name this module
rejects but the spec allows is an image that works everywhere except here. Every
case below is therefore pinned as a behavioural contract, not as a convenience.

Nothing here touches a database, a session, or the application — a value object
that needed any of those would already be the wrong shape.

"""

from dataclasses import FrozenInstanceError
from typing import Final

import pytest

from wooloo.domain.repositories.exceptions import InvalidRepositoryName, RepositoryError
from wooloo.domain.repositories.name import RepositoryName

_LONG_COMPONENT: Final = "a" * 30

MAX_REALISTIC_NAME: Final = "/".join([_LONG_COMPONENT] * 8)
"""A 247-character name: eight deep, thirty per component.

Sits just under the 255-character ceiling real clients impose, so it stands in
for the longest name that could plausibly arrive rather than an arbitrary one.
"""

MAX_LENGTH: Final = 255
"""The longest name the value object accepts.

Spelled out here rather than imported from `name.py`, so that moving the bound in
the source has to be a deliberate change to this contract as well.
"""

MAX_ECHOED_INPUT: Final = 100
"""
Characters of a rejected name the error message may quote back, likewise spelled
out rather than imported.

"""

VALID_NAMES: Final = [
    pytest.param("library/nginx", "library/nginx", id="two-components"),
    pytest.param("acme/backend-api", "acme/backend-api", id="single-hyphen"),
    pytest.param("team_a/service_v2", "team_a/service_v2", id="single-underscore"),
    pytest.param("nginx", "nginx", id="single-component"),
    pytest.param("library/nginx.io", "library/nginx.io", id="dot-separator"),
    pytest.param("v2/app123", "v2/app123", id="digits"),
    pytest.param("acme/team__b/svc-1--x", "acme/team__b/svc-1--x", id="three-components"),
    pytest.param(
        "x1.y2_z3__w4-v5--u6",
        "x1.y2_z3__w4-v5--u6",
        id="every-separator-in-one-component",
    ),
    pytest.param(MAX_REALISTIC_NAME, MAX_REALISTIC_NAME, id="max-realistic-length"),
    pytest.param("  library/nginx  ", "library/nginx", id="surrounding-spaces-stripped"),
    pytest.param("\tlibrary/nginx\n", "library/nginx", id="surrounding-tab-newline-stripped"),
]
"""`(raw input, expected stored value)` for names the OCI grammar accepts.

The pairs are what make the whitespace rows meaningful: stripping is the only
normalization the value object performs, so the expected value differs from the
input exactly when — and only when — the input had surrounding whitespace.
"""

INVALID_NAMES: Final = [
    pytest.param("Library/Nginx", id="uppercase"),
    pytest.param("ACME/backend", id="uppercase-first-component"),
    pytest.param("my repo", id="internal-space"),
    pytest.param("@@invalid", id="disallowed-characters"),
    pytest.param("acme/", id="trailing-slash"),
    pytest.param("/acme", id="leading-slash"),
    pytest.param("acme//backend", id="doubled-slash"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces-only"),
    pytest.param("\n\t ", id="mixed-whitespace-only"),
    pytest.param("acme /backend", id="space-before-separator"),
    pytest.param("acme/ backend", id="space-after-separator"),
    pytest.param("  acme /backend  ", id="internal-space-with-surrounding-space"),
    pytest.param("acme\n/backend", id="newline-before-separator"),
    pytest.param("-nginx", id="leading-hyphen"),
    pytest.param("nginx-", id="trailing-hyphen"),
    pytest.param("acme/.hidden", id="leading-dot"),
    pytest.param("acme/_x", id="leading-underscore"),
    pytest.param("acme/back___end", id="tripled-underscore"),
    pytest.param("acme/back..end", id="doubled-dot"),
]
"""Names no OCI client would accept, each rejected for a distinct reason.

The whitespace rows carry the most weight. `strip()` removes surrounding
whitespace *from the whole string*, so a space adjacent to a `/` survives it and
must still be rejected — see the dedicated tests below.
"""


# --------------------------------------------------------------------------- #
# Acceptance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("raw", "expected"), VALID_NAMES)
def test_accepts_names_the_oci_grammar_allows(raw: str, expected: str) -> None:
    """Every spec-legal name constructs and stores its stripped form.

    Asserting the stored value, not merely the absence of an exception, is what
    stops a future "normalization" (case folding, separator rewriting) from
    silently changing what gets persisted while the tests stay green.
    """
    name = RepositoryName(raw)

    assert name.value == expected


@pytest.mark.parametrize(("raw", "expected"), VALID_NAMES)
def test_str_returns_the_validated_name(raw: str, expected: str) -> None:
    """Formatting a name yields the name, so log lines and URLs stay usable.

    Without `__str__`, an f-string would interpolate the dataclass repr —
    `RepositoryName(value='library/nginx')` — into paths and log fields.
    """
    assert str(RepositoryName(raw)) == expected
    assert f"{RepositoryName(raw)}" == expected


# --------------------------------------------------------------------------- #
# Rejection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", INVALID_NAMES)
def test_rejects_names_the_oci_grammar_forbids(raw: str) -> None:
    """
    Every name a real client would refuse is refused here, at construction.

    """
    with pytest.raises(InvalidRepositoryName):
        RepositoryName(raw)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("acme /backend", id="space-before-separator"),
        pytest.param("acme/ backend", id="space-after-separator"),
        pytest.param("  acme /backend  ", id="internal-space-with-surrounding-space"),
        pytest.param("acme\n/backend", id="newline-before-separator"),
        pytest.param("lib rary/nginx", id="space-inside-component"),
    ],
)
def test_rejects_whitespace_that_is_not_purely_surrounding(raw: str) -> None:
    """Stripping must never rescue a name whose whitespace is internal.

    This is the sharp edge of allowing `strip()` at all. `"  acme /backend  "`
    loses its outer padding and is still illegal, because the space sits next to
    the separator. A per-component `strip()`, or a `replace(" ", "")`, would turn
    each of these into a valid name and let the registry store something no
    client can address. The last row pins the same rule inside a component.
    """
    with pytest.raises(InvalidRepositoryName):
        RepositoryName(raw)


def test_rejects_a_component_ending_in_a_newline() -> None:
    """A trailing newline inside a component must not slip through the anchor.

    Python's `$` matches before a final newline, so `re.match` would accept the
    component `"acme\\n"` from `"acme\\n/backend"` — and the outer `strip()` does
    not touch it, because it is not at either end of the whole string. Only
    `fullmatch` closes that hole. Swapping `fullmatch` for `match` fails here and
    nowhere else in this file.
    """
    with pytest.raises(InvalidRepositoryName):
        RepositoryName("acme\n/backend")


def test_rejection_message_quotes_the_caller_s_original_input() -> None:
    """The error must echo what was sent, not a half-normalized rewrite.

    An operator debugging a rejected push needs to see their own string,
    whitespace included — a message quoting the stripped form would hide the
    very padding that is often the cause.
    """
    raw = "  Library/Nginx  "

    with pytest.raises(InvalidRepositoryName) as caught:
        RepositoryName(raw)

    assert repr(raw) in str(caught.value)


def test_rejection_is_catchable_as_the_shared_domain_base() -> None:
    """`InvalidRepositoryName` stays inside the `RepositoryError` hierarchy.

    The API layer maps domain failures by catching the base, so re-parenting
    this exception would silently turn a 400 into an unhandled 500.
    """
    with pytest.raises(RepositoryError):
        RepositoryName("Library/Nginx")


# --------------------------------------------------------------------------- #
# Length bound
# --------------------------------------------------------------------------- #


def test_accepts_a_name_of_exactly_the_maximum_length() -> None:
    """255 characters is inside the bound; only 256 may be refused.

    Built from real components rather than one run of `a`, so the grammar still
    has to walk the whole name to accept it.
    """
    raw = f"{MAX_REALISTIC_NAME}/{'a' * (MAX_LENGTH - len(MAX_REALISTIC_NAME) - 1)}"

    assert len(raw) == MAX_LENGTH
    assert RepositoryName(raw).value == raw


def test_rejects_a_name_one_character_past_the_maximum() -> None:
    """256 characters is refused, and the message quotes the length, not the name.

    The name is spec-legal, so nothing but the length check can reject it — which
    is also what pins that the check runs *before* the grammar walk.

    How large the input is is its only relevant property here, so quoting any of
    it would put an unbounded, attacker-controlled string into both the 400 body
    and the log pipeline for no diagnostic gain.
    """
    raw = "a" * (MAX_LENGTH + 1)

    with pytest.raises(InvalidRepositoryName) as caught:
        RepositoryName(raw)

    message = str(caught.value)
    assert str(MAX_LENGTH) in message
    assert str(MAX_LENGTH + 1) in message
    assert "a" * (MAX_ECHOED_INPUT + 1) not in message


def test_a_long_invalid_name_is_quoted_only_up_to_the_echo_bound() -> None:
    """
    A grammar failure echoes a bounded prefix and the original length, never the
    whole input.

    """
    raw = "A" * 200

    with pytest.raises(InvalidRepositoryName) as caught:
        RepositoryName(raw)

    message = str(caught.value)
    assert repr(f"{'A' * MAX_ECHOED_INPUT}…[{len(raw)}]") in message
    assert raw not in message


def test_whitespace_padding_cannot_smuggle_an_unbounded_name_into_the_message() -> None:
    """The echo bound, not the length check, is what keeps the message small.

    The length check measures the *stripped* value, so an input padded with
    whitespace clears it at any size and still reaches the grammar failure
    carrying every byte the sender sent. Without truncation there, one request
    writes as much output as its author cares to generate.
    """
    raw = f"{' ' * 500}@@invalid"

    with pytest.raises(InvalidRepositoryName) as caught:
        RepositoryName(raw)

    message = str(caught.value)
    assert f"…[{len(raw)}]" in message
    assert raw not in message
    assert len(message) < 2 * MAX_ECHOED_INPUT


# --------------------------------------------------------------------------- #
# Value semantics
# --------------------------------------------------------------------------- #


def test_equal_names_compare_equal() -> None:
    """
    Two instances built from the same name are interchangeable.

    """
    assert RepositoryName("library/nginx") == RepositoryName("library/nginx")


def test_whitespace_variants_compare_equal() -> None:
    """Padding is invisible to equality, because it is stripped before storage.

    Callers must not have to normalize before comparing — that duty is exactly
    what the value object exists to absorb.
    """
    assert RepositoryName("  library/nginx  ") == RepositoryName("library/nginx")


def test_different_names_compare_unequal() -> None:
    """
    Equality is by value, so distinct names must not collapse together.

    """
    assert RepositoryName("library/nginx") != RepositoryName("library/redis")


def test_a_name_is_not_equal_to_the_bare_string() -> None:
    """A validated name is not interchangeable with an unvalidated `str`.

    If this ever passed, code could compare a `RepositoryName` against raw user
    input and get a match, defeating the point of holding proof of validation.

    The annotation widens the literal to `object` so the comparison survives a
    type checker that would otherwise reject it as provably non-overlapping —
    the runtime check is the point, since untyped call sites are where such a
    comparison would actually occur.
    """
    unvalidated: object = "library/nginx"

    assert RepositoryName("library/nginx") != unvalidated


def test_equal_names_hash_equal_and_deduplicate_in_a_set() -> None:
    """
    Whitespace variants of one name are a single set member, not two.

    """
    padded = RepositoryName("  library/nginx  ")
    bare = RepositoryName("library/nginx")

    assert hash(padded) == hash(bare)
    assert {padded, bare} == {bare}


def test_a_name_works_as_a_dictionary_key() -> None:
    """Names index caches and lookup tables, so key behaviour must be stable.

    A whitespace variant must reach the same entry, otherwise a padded name
    would create a duplicate cache slot for the same repository.
    """
    by_name = {RepositoryName("library/nginx"): "first"}

    by_name[RepositoryName("  library/nginx  ")] = "second"

    assert by_name == {RepositoryName("library/nginx"): "second"}


def test_a_constructed_name_cannot_be_mutated() -> None:
    """Immutability is what makes the validation permanent.

    A mutable `value` would let a caller assign an illegal name onto an instance
    that has already been vouched for, and every downstream check has been
    dropped precisely because holding the type is supposed to be proof.
    """
    name = RepositoryName("library/nginx")

    with pytest.raises(FrozenInstanceError):
        name.value = "Library/Nginx"  # type: ignore[misc]

    assert name.value == "library/nginx"
