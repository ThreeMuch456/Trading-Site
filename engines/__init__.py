"""In-process engine registry + router — the heart of the unified (single-deploy) desk.

Full-merge architecture (replaces the earlier aggregator-of-six-services design):
each vendored tool now computes, inside this process, the exact JSON its former
standalone HTTP endpoint returned, and registers the path(s) under which the desk's
source adapters request it. The desk transport shim (``desk/sources/__init__.py``)
calls :func:`produce` instead of making a network request; this module strips the
logical ``local://<engine>`` host and dispatches the path to the registered handler.

Because the JSON contracts are preserved byte-for-byte, the desk's source adapters,
their ``merge()`` functions, every template, and the whole test-suite are UNCHANGED
— only the transport moved from HTTP to a local function call.

Registering an engine (done by each ``engines/<tool>.py`` on import):
  - ``@route("/api/state")`` / ``@route_prefix("/api/ticker/")`` — map a path to a
    handler ``fn(params)`` (or ``fn(tail, params)`` for a prefix) returning the JSON.
  - ``serve("zerodte", predicate)`` — declare that source name is produced locally
    when ``predicate()`` is true (e.g. a Massive key is present). ``desk.config``
    reads this to decide live-vs-sample per source.

Every engine import is guarded: a broken or dependency-missing engine degrades that
one source to sample data instead of taking down the app.
"""

import logging
import urllib.parse

log = logging.getLogger("engines")

# path suffix -> fn(params: dict) -> dict (the JSON the old endpoint returned)
_ROUTES: dict = {}
# path prefix -> fn(tail: str, params: dict) -> dict  (for path params like /api/ticker/NVDA)
_PREFIX_ROUTES: dict = {}
# source name -> predicate() -> bool  (is this source producible locally right now?)
_SERVES: dict = {}
# functions fn(scheduler, tz) that add an engine's background jobs to the desk scheduler
_JOB_REGISTRARS: list = []

# Engine submodules imported by load_all(). Populated as tools are ported. Each,
# on import, registers its routes and source name. Order is irrelevant.
_ENGINE_MODULES: list[str] = [
    "engines.stock",        # Phase 1
    "engines.flow",         # Phase 2
    # "engines.zerodte",    # Phase 3
    # "engines.agent",      # Phase 4
    # "engines.structure",  # Phase 5
]

_loaded = False


def route(suffix: str):
    """Register an exact-path handler: fn(params: dict) -> dict."""
    def deco(fn):
        _ROUTES[suffix] = fn
        return fn
    return deco


def route_prefix(prefix: str):
    """Register a prefix handler for paths with a trailing segment (e.g. a ticker):
    fn(tail: str, params: dict) -> dict, where tail is the path after the prefix."""
    def deco(fn):
        _PREFIX_ROUTES[prefix] = fn
        return fn
    return deco


def serve(name: str, predicate) -> None:
    """Declare that source `name` is produced in-process when predicate() is true."""
    _SERVES[name] = predicate


def jobs(fn):
    """Register a function fn(scheduler, tz) that adds an engine's background jobs
    (e.g. a scanner/poller/scheduled brief) to the desk scheduler. Called by
    desk.schedule.start() via add_jobs()."""
    _JOB_REGISTRARS.append(fn)
    return fn


def add_jobs(scheduler, tz=None) -> None:
    """Let every registered engine attach its background jobs to the desk scheduler.
    Guarded per-engine so one engine's job wiring can't break the others."""
    load_all()
    for fn in _JOB_REGISTRARS:
        try:
            fn(scheduler, tz)
        except Exception:
            log.warning("engine job registrar %r failed", getattr(fn, "__module__", fn),
                        exc_info=True)


def load_all() -> None:
    """Import every engine module once, guarding each so one failure is isolated."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _ENGINE_MODULES:
        try:
            __import__(mod)
        except Exception:
            log.warning("engine %s failed to load — its source will serve sample "
                        "data", mod, exc_info=True)


def available_sources() -> set:
    """Source names with a registered engine whose prerequisites are currently met."""
    load_all()
    out = set()
    for name, pred in _SERVES.items():
        try:
            if pred():
                out.add(name)
        except Exception:
            log.warning("availability check for %s failed", name, exc_info=True)
    return out


def produce(url: str, token: str = "", header: str = "", timeout: float = 4.0) -> dict:
    """Dispatch a logical ``local://<engine>/api/...`` URL to its handler and return
    the JSON. `token`/`header`/`timeout` are accepted for signature-compatibility with
    the old HTTP transport but are unused in-process. Raises LookupError if no route
    matches (treated by the caller as a degraded source, i.e. sample fallback)."""
    load_all()
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    params = dict(urllib.parse.parse_qsl(parsed.query))
    fn = _ROUTES.get(path)
    if fn is not None:
        return fn(params)
    for prefix, pfn in _PREFIX_ROUTES.items():
        if path.startswith(prefix):
            return pfn(path[len(prefix):], params)
    raise LookupError(f"no local engine route for {url!r}")
