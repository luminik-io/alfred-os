# Memory providers

Alfred ships a single-host memory layer: a runner can call
`memory.recall(...)` before a firing to surface lessons earlier
firings learned, and `memory.reflect(...)` afterwards to file new
ones. The default chain is `redis,fleet`: Redis Agent Memory Server stores the
semantic lessons Alfred recalls, while FleetBrain keeps the local operational
ledger and review queue.

The bundled Redis server binds to loopback by default. Nothing is sent to a
hosted memory service. If you configure Alfred's usage counter, it sends
anonymous aggregate counts only and can be disabled with `alfred telemetry off`.

This doc covers the **provider layer** above the brain: how to chain
memory backends so an agent reads from Redis first and still records local
operational state in FleetBrain.

## When to use this

Most users can leave the default alone. Reach for the provider layer when one
of these is true:

- You maintain your own personal knowledge base (notes app with a
  CLI, a local search index, a vector store you built years ago) and
  want Alfred firings to consult it as a fallback for older context.
- You want to disable runtime recall and reflection without ripping out the
  call sites (set `ALFRED_MEMORY_PROVIDERS=null`).
- You're writing a custom provider for a downstream fleet (e.g. a
  team wiki shim) and want to chain it behind Redis or FleetBrain.
- You run Redis Agent Memory Server on a different loopback port or host and
  want Alfred to use that endpoint.

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
| `redis` | `lib/memory/redis_agent_memory.py` | yes | Primary semantic memory client. Defaults to the bundled loopback Agent Memory Server. |
| `fleet` | `lib/memory/providers.py` | yes | Local operational ledger and review queue. SQLite under `$ALFRED_HOME`. |
| `gbrain` | `lib/memory/gbrain_stub.py` | no | Optional subprocess shim into the operator's personal knowledge base CLI. Not bundled functionality. |
| `null` | `lib/memory/providers.py` | no | No-op. `recall` returns `[]`, `reflect` raises. Used when `ALFRED_MEMORY_PROVIDERS=null` or the env var is explicitly empty. |

## Configuration

Two env vars drive the chain:

```sh
# Consult order. Comma-separated. Whitespace and case insensitive.
# Unset default -> redis,fleet.
ALFRED_MEMORY_PROVIDERS=redis,fleet

# Optional: path to the operator's personal knowledge base CLI.
# Read by gbrain_stub; the binary is invoked with a JSON payload on
# stdin and must emit a JSON list of lessons on stdout.
ALFRED_GBRAIN_BIN=/usr/local/bin/gbrain

# Redis Agent Memory Server. Leave URL unset to use ALFRED_AMS_HOST/PORT.
ALFRED_REDIS_MEMORY_URL=http://127.0.0.1:8088
ALFRED_REDIS_MEMORY_NAMESPACE=alfred
ALFRED_REDIS_MEMORY_USER_ID=operator-id
ALFRED_REDIS_MEMORY_TOKEN=
ALFRED_REDIS_MEMORY_SEARCH_MODE=semantic

# Bundled local server defaults.
ALFRED_AMS_HOST=127.0.0.1
ALFRED_AMS_PORT=8088
ALFRED_AMS_REDIS_URL=redis://127.0.0.1:6379/0
ALFRED_AMS_EMBEDDING_MODEL=ollama/mxbai-embed-large
ALFRED_AMS_EMBEDDING_DIM=1024
ALFRED_AMS_GENERATION_MODEL=ollama/llama3.2:1b
```

Sample shell config for adding a read-only personal knowledge base behind the
default memory stack:

```sh
export ALFRED_MEMORY_PROVIDERS=redis,fleet,gbrain
export ALFRED_GBRAIN_BIN=/usr/local/bin/gbrain
```

Sample shell config for "memory off":

```sh
export ALFRED_MEMORY_PROVIDERS=null
```

Sample shell config for a custom Agent Memory Server endpoint:

```sh
export ALFRED_MEMORY_PROVIDERS=redis,fleet
export ALFRED_REDIS_MEMORY_URL=http://127.0.0.1:9090
export ALFRED_REDIS_MEMORY_NAMESPACE=alfred
```

Keep `fleet` in the chain unless you are deliberately running without the local
review queue and operational ledger. The default reflection mode stores
engine-proposed memories as reviewable FleetBrain candidates before they enter
recall. Redis is the promoted lesson store; FleetBrain is the queue and ledger.

Check the local server:

```sh
alfred brain ams-status
alfred brain redis-status
alfred brain ams-status --json
```

Mirror reviewed local lessons into Redis explicitly:

```sh
alfred brain redis-sync --dry-run
alfred brain redis-sync --codename lucius --repo your-org/api
```

The sync path only reads trusted lessons from the fleet-brain. It does not
upload raw transcripts, event logs, or unreviewed memory candidates.

## How chaining works

`ChainedMemoryProvider` consults providers in declared order:

1. **`recall`** asks each provider in turn and returns the first
   non-empty list. A provider that raises is logged and skipped --
   one flaky backend cannot break the firing.
2. **`reflect`** writes to the first provider that does not raise
   `NotImplementedError`. Read-only providers earlier in the chain
   are skipped silently.

Worked trace for `ALFRED_MEMORY_PROVIDERS=redis,fleet,gbrain`:

```
firing "lucius" starts, asks memory.recall(codename="lucius", repo="acme-org/api"):
  -> redis.recall(...) returns [Lesson("GraphQL schema lives in src/schema.graphql")]
  -> chain stops there; fleet and gbrain are not consulted for recall

firing finishes, asks memory.reflect(codename=..., repo=..., body="..."):
  -> redis.reflect(...) writes the promoted lesson to Agent Memory Server
  -> FleetBrain remains available for candidates, firings, and reliability rows
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

Now `ALFRED_MEMORY_PROVIDERS=redis,fleet,team_wiki` works.

## Privacy and scope

- The `gbrain` provider is the operator's optional personal knowledge
  base. It is **not** bundled with Alfred. The shim only knows the
  path the operator gives it; if the binary is missing, recall
  returns empty and the chain keeps working.
- Nothing in the default memory layer phones home. Redis Agent Memory Server
  binds to loopback, and FleetBrain is a SQLite file under `$ALFRED_HOME`.
- `alfred brain redis-sync` remains available for carrying older reviewed
  FleetBrain lessons into Redis.
- Read-only providers cannot exfiltrate FleetBrain. Writes flow
  the other direction (to the first writer in the chain), never out
  to gbrain.

## Deferred

- **Cross-provider result ranking.** Redis Agent Memory handles semantic recall.
  A later chain can rank Redis, FleetBrain, and read-only provider results
  together before prompt injection.
- **Reflect-everywhere.** Today `reflect` writes to the first
  writable provider only. A "broadcast" mode that fans the write
  out to every writer is intentionally out of scope until users prove
  they want Redis and FleetBrain written on every firing.
- **Per-provider limits.** `limit` is passed verbatim to every
  provider in the chain; a smarter chain could split the budget.
- **Cache.** No caching between calls. Each provider is hit fresh on
  every `recall`. Good enough at single-host single-operator scale.
