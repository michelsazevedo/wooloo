"""
Unit tests for `ListRepositoriesUseCase`.

This use case owns exactly one policy the persistence port refuses to make: what a
caller's `limit` and `offset` are allowed to be. The port applies them verbatim, so
if the clamp is missing or mis-applied nothing downstream will catch it — a single
`?limit=1000000` request simply reads the table. These tests therefore assert the
clamp from both sides at once: the value the store was *called* with, and the value
the returned page *reports*.

That pairing is deliberate. The store double echoes its arguments back onto the
page, mirroring the real store's contract, so a clamp applied to the store call but
not reflected in the page — or a page rewritten after the fact to show the caller's
raw request — fails here rather than misleading a client about the page size it
actually received.

"""

import pytest
from _fakes import FakeRepositoryStore, PageRequest, make_repository
from structlog.typing import EventDict

from wooloo.application.use_cases import list_repositories
from wooloo.application.use_cases.list_repositories import ListRepositoriesUseCase

DEFAULT_LIMIT = 20

DEFAULT_OFFSET = 0

LIMIT_CLAMP_CASES = [
    (500, 100),
    (101, 100),
    (100, 100),
    (21, 21),
    (1, 1),
    (0, 1),
    (-7, 1),
]
"""`(requested, applied)` page sizes.

Includes both bounds and the values immediately inside and outside them. The
in-range rows are as load-bearing as the out-of-range ones: without `(21, 21)` an
implementation that ignored its argument and always passed 100 would satisfy every
remaining case.
"""

OFFSET_CLAMP_CASES = [
    (-5, 0),
    (-1, 0),
    (0, 0),
    (40, 40),
    (10_000, 10_000),
]
"""`(requested, applied)` offsets.

Only the lower bound is enforced. `10_000` pins the absence of an upper one: an
offset past the end of the collection is a legitimate request that yields an empty
page, so clamping it would turn a correct empty result into a wrong non-empty one.
"""


async def test_execute_applies_the_documented_defaults() -> None:
    """A bare `execute()` pages 20 rows from the start.

    The defaults live on this signature, not in the API layer, so a caller that is
    not an HTTP request — a background job, a future gRPC surface — gets the same
    bounded page rather than an unbounded read.
    """
    store = FakeRepositoryStore()

    page = await ListRepositoriesUseCase(store).execute()

    assert store.list_calls == [PageRequest(limit=DEFAULT_LIMIT, offset=DEFAULT_OFFSET)]
    assert page.limit == DEFAULT_LIMIT
    assert page.offset == DEFAULT_OFFSET


@pytest.mark.parametrize(("requested", "applied"), LIMIT_CLAMP_CASES)
async def test_execute_clamps_limit_into_range(requested: int, applied: int) -> None:
    """The clamped page size reaches the store *and* is reported back to the caller.

    Both assertions are needed and neither is redundant. The first fails if the
    clamp is dropped — the store would receive 500 and read 500 rows. The second
    fails if the clamp is applied on the way down but the page is rebuilt from the
    caller's raw request on the way up, which would leave a client that asked for
    500 and received 100 items believing it had seen a full page and that no more
    exist.
    """
    store = FakeRepositoryStore()

    page = await ListRepositoriesUseCase(store).execute(limit=requested)

    assert store.list_calls == [PageRequest(limit=applied, offset=DEFAULT_OFFSET)]
    assert page.limit == applied


@pytest.mark.parametrize(("requested", "applied"), OFFSET_CLAMP_CASES)
async def test_execute_clamps_offset_to_a_non_negative_value(requested: int, applied: int) -> None:
    """A negative offset becomes 0; every non-negative one is honored as given.

    A negative offset reaching SQL is not merely odd, it is a syntax error the
    database rejects — so the clamp is what turns a nonsensical request into an
    ordinary first page instead of a 500.
    """
    store = FakeRepositoryStore()

    page = await ListRepositoriesUseCase(store).execute(offset=requested)

    assert store.list_calls == [PageRequest(limit=DEFAULT_LIMIT, offset=applied)]
    assert page.offset == applied


async def test_execute_clamps_both_bounds_in_the_same_call() -> None:
    """The two clamps are independent and both apply at once.

    Guards against a single shared bound: a clamp helper accidentally used for both
    values would pass every single-argument case above and only diverge here, where
    the correct answers (100 and 0) differ from each other.
    """
    store = FakeRepositoryStore()

    page = await ListRepositoriesUseCase(store).execute(limit=500, offset=-5)

    assert store.list_calls == [PageRequest(limit=100, offset=0)]
    assert page.limit == 100
    assert page.offset == 0


async def test_execute_returns_the_store_s_page_unchanged() -> None:
    """The store's own page object travels out untouched.

    Identity — `is` — is what pins it. Rebuilding an equal `RepositoryPage` here
    would pass an equality check while giving this layer a place to quietly alter
    `total` or reorder `items`, both of which are the store's answers to give.
    """
    items = [make_repository("acme/backend-api"), make_repository("acme/frontend")]
    store = FakeRepositoryStore(page_items=items, page_total=57)

    page = await ListRepositoriesUseCase(store).execute()

    assert page is store.list_results[0]
    assert page.items == items
    assert page.total == 57


@pytest.mark.parametrize(("requested", "applied"), LIMIT_CLAMP_CASES)
async def test_execute_never_logs(
    requested: int, applied: int, captured_logs: list[EventDict]
) -> None:
    """Listing emits no structured events, on any input including clamped ones.

    Listing is neither a mutation nor a single-entity retrieval, so a line per call
    would emit noise proportional to read traffic while telling an operator nothing
    they could act on; request-level observability already covers this path. The
    clamped inputs are included on purpose — "the caller asked for 500, we gave
    100" is exactly the event someone would be tempted to add here.
    """
    store = FakeRepositoryStore()

    await ListRepositoriesUseCase(store).execute(limit=requested)

    assert captured_logs == []


def test_the_module_holds_no_logger() -> None:
    """The absence of logging is structural, not merely unexercised.

    The behavioral test above proves no event was emitted on the paths it drove;
    this proves the module has no logger to emit one from at all, so the property
    holds on paths no test drives. Together they are the evidence that the silence
    is deliberate rather than an oversight — the module's own docstring makes that
    claim, and this is what keeps it honest.
    """
    module_globals = vars(list_repositories)

    assert "logger" not in module_globals
    assert "structlog" not in module_globals
    assert "logging" not in module_globals
