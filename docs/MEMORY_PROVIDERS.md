# Memory providers

Alfred ships a single-host memory layer: a runner can call
`memory.recall(...)` before a firing to surface lessons earlier
firings learned, and `memory.reflect(...)` afterwards to file new
ones. The default backend is the in-tree
[`fleet_brain`](./FLEET_BRAIN.md) SQLite store. Nothing leaves the
host, no telemetry, no cloud sync.

This doc covers the **provider layer** above the brain: how to chain
multiple memory backends so an agent reads from the fleet-brain first
and falls through to a personal knowledge base on a miss.

## When to use this

You probably don't need it. The shipping default is the fleet-brain
alone, and most operators stop there. Reach for the provider layer
when one of these is true:

- You maintain your own personal knowledge base (notes app with a
  CLI, a local search index, a vector store you built years ago) and
  want Alfred firings to consult it as a fallback for older context.
- You want to disable the fleet-brain entirely without ripping out
  the recall/reflect call sites (set
  `ALFRED_MEMORY_PROVIDERS=null`).
- You're writing a custom provider for a downstream fleet (e.g. a
  team wiki shim) and want to chain it behind the fleet-brain.
- You already run Redis Agent Memory Server and want Alfred to consult
  it as an optional second memory surface.

## The Protocol

Providers implement a tiny Protocol (`lib/memory/__init__.py`):

```python
class MemoryProvider(Protocol):
    name: str

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]: ...

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        severity: Severity = "info",
        firing_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson: ...
```

Runners depend on the Protocol, never on a concrete class.
Read-only providers raise `NotImplementedError` from `reflect`; the
chain wrapper catches it and tries the next writer.

## Built-in providers

| Name | File | Writable? | Notes |
|---|---|---|---|
| `fleet` | `lib/memory/providers.py` | yes | Wraps `fleet_brain.FleetBrain`. SQLite under `$ALFRED_HOME`. |
| `gbrain` | `lib/memory/gbrain_stub.py` | no | Optional subprocess shim into the operator's personal knowledge base CLI. Not bundled functionality. |
| `redis` | `lib/memory/redis_agent_memory.py` | yes | Optional bridge to Redis Agent Memory Server. Not installed or started by Alfred. |
| `null` | `lib/memory/providers.py` | no | No-op. `recall` returns `[]`, `reflect` raises. Default when env is empty. |

## Configuration

Two env vars drive the chain:

```sh
# Consult order. Comma-separated. Whitespace and case insensitive.
# Unset (the OSS default) -> fleet-brain only.
ALFRED_MEMORY_PROVIDERS=fleet,gbrain

# Optional: path to the operator's personal knowledge base CLI.
# Read by gbrain_stub; the binary is invoked with a JSON payload on
# stdin and must emit a JSON list of lessons on stdout.
ALFRED_GBRAIN_BIN=/usr/local/bin/gbrain

# Optional: Redis Agent Memory Server.
ALFRED_REDIS_MEMORY_URL=http://127.0.0.1:8000
ALFRED_REDIS_MEMORY_NAMESPACE=alfred
ALFRED_REDIS_MEMORY_USER_ID=operator-id
ALFRED_REDIS_MEMORY_TOKEN=
```

Sample shell config for a chained setup:

```sh
export ALFRED_MEMORY_PROVIDERS=fleet,gbrain
export ALFRED_GBRAIN_BIN=/usr/local/bin/gbrain
```

Sample shell config for "memory off":

```sh
export ALFRED_MEMORY_PROVIDERS=null
```

Sample shell config for Redis AMS as a fallback after the local
fleet-brain:

```sh
export ALFRED_MEMORY_PROVIDERS=fleet,redis
export ALFRED_REDIS_MEMORY_URL=http://127.0.0.1:8000
export ALFRED_REDIS_MEMORY_NAMESPACE=alfred
```

## How chaining works

`ChainedMemoryProvider` consults providers in declared order:

1. **`recall`** asks each provider in turn and returns the first
   non-empty list. A provider that raises is logged and skipped --
   one flaky backend cannot break the firing.
2. **`reflect`** writes to the first provider that does not raise
   `NotImplementedError`. Read-only providers earlier in the chain
   are skipped silently.

Worked trace for `ALFRED_MEMORY_PROVIDERS=fleet,gbrain`:

```
firing "lucius" starts, asks memory.recall(codename="lucius", repo="acme-org/api"):
  -> fleet.recall(...) returns [Lesson("GraphQL schema lives in src/schema.graphql")]
  -> chain stops there; gbrain is NOT consulted

firing finishes, asks memory.reflect(codename=..., repo=..., body="..."):
  -> fleet.reflect(...) succeeds; lesson recorded in $ALFRED_HOME/fleet-brain.db
  -> gbrain.reflect would raise NotImplementedError but is not reached
```

If the fleet had been empty for that (codename, repo):

```
firing "lucius" starts, asks memory.recall(...):
  -> fleet.recall(...) returns []
  -> gbrain.recall(...) shells out to $ALFRED_GBRAIN_BIN, returns
     [Lesson("older notes about acme-org/api auth")]
  -> chain returns the gbrain result

firing finishes, asks memory.reflect(...):
  -> fleet.reflect(...) succeeds first (gbrain is read-only and is
     after fleet in the chain anyway)
```

## Writing a custom provider

Drop a new file under `lib/memory/`, implement the Protocol, and
register it:

```python
# lib/memory/team_wiki.py
from dataclasses import dataclass

@dataclass
class TeamWikiProvider:
    name: str = "team_wiki"

    def recall(self, *, query=None, codename=None, repo=None, limit=5):
        # call your wiki API, map results to Lesson objects
        ...

    def reflect(self, **_):
        raise NotImplementedError("team_wiki is read-only")
```

Then in `lib/memory/config.py`:

```python
from .team_wiki import TeamWikiProvider

PROVIDER_REGISTRY["team_wiki"] = lambda env: TeamWikiProvider()
```

Now `ALFRED_MEMORY_PROVIDERS=fleet,team_wiki` works.

## Privacy and scope

- The `gbrain` provider is the operator's optional personal knowledge
  base. It is **not** bundled with Alfred. The shim only knows the
  path the operator gives it; if the binary is missing, recall
  returns empty and the chain keeps working.
- Nothing in the memory layer phones home. The fleet-brain is a
  SQLite file under `$ALFRED_HOME`; the gbrain shim invokes a
  subprocess the operator already installed locally.
- The `redis` provider only runs when the operator opts in by env.
  Alfred does not install Redis, start Redis AMS, or make it a hard
  runtime dependency.
- Read-only providers cannot exfiltrate the fleet-brain. Writes flow
  the other direction (to the first writer in the chain), never out
  to gbrain.

## Deferred

- **Semantic recall.** The Protocol takes a `query` string but the
  fleet-brain v1 only does substring matching. Vector similarity is
  v2 (PGLite + pgvector, see `docs/FLEET_BRAIN.md`).
- **Reflect-everywhere.** Today `reflect` writes to the first
  writable provider only. A "broadcast" mode that fans the write
  out to every writer is intentionally out of scope until users prove
  they want Redis and fleet-brain written on every firing.
- **Per-provider limits.** `limit` is passed verbatim to every
  provider in the chain; a smarter chain could split the budget.
- **Cache.** No caching between calls. Each provider is hit fresh on
  every `recall`. Good enough at single-host single-operator scale.
