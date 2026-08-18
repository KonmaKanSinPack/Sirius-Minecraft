# sirius-bridge

NeoForge mod running on the **real Minecraft client**, acting as the "eyes and hands" of the Sirius AI brain: screenshot capture, input injection, and event push. See `../sirius-technical.md` §8.2 for the full spec.

> Current status: **M1-B complete** - WebSocket server (localhost + token handshake), capability negotiation and the frame dispatch skeleton are implemented and verified against the real `sirius-brain` Python client. The tool implementations themselves (screenshot/getStats/world.query/...) land in M1-C.

## Versions

| Component | Version |
|---|---|
| Minecraft | 1.21.1 |
| NeoForge | 21.1.248 (1.21.1 line = 21.1.x) |
| ModDevGradle | 2.0.141 |
| Gradle wrapper | 9.2.0 |
| Java | 21 (JDK 21 required) |
| Java-WebSocket | 1.5.7 (bundled via NeoForge jar-in-jar) |

> 2026-08-18: NeoForge dependency aligned from 21.1.233 to **21.1.248** to match the
> HMCL test client instance `1.21.1-Sirius` (`.minecraft/versions/1.21.1-Sirius`),
> whose NeoForge is 21.1.248.

## The bridge server (M1-B)

When the client reaches the title screen, the mod starts a WebSocket server:

- **Address**: `ws://127.0.0.1:8765` - bound to loopback only, never reachable
  from the network. Port is configurable (see below).
- **Token handshake**: the first frame on every connection must be
  `{"type":"hello","token":"...","protocol_version":"1.0"}`. On a matching token
  the server replies `{"type":"hello_ack","ok":true,"protocol_version":"1.0"}`.
  A wrong token, any other frame first, or 10 s of silence closes the
  connection (close code 1008). Token comparison is constant-time.
- **Token location**: `config/sirius_bridge.toml` (relative to the game
  directory). On first launch a random 64-hex-char token is generated and
  written there; it is also printed to `logs/sirius_bridge.log` (one `START`
  line per launch). Rotate it by deleting the line (or setting `token = ""`)
  and restarting the game.
- **Config**: `config/sirius_bridge.toml` with `port` (default 8765) and `token`.
- **Audit log**: `logs/sirius_bridge.log` - one line per server start/stop,
  connect/disconnect, hello success/failure and every request (with the
  resulting error code).
- **Capabilities**: `capabilities/list` returns the 12 frozen capabilities
  (name/version/input_schema) assembled from the schema JSON files copied into
  the jar at build time from `../sirius-brain/schema` (single source of truth;
  see "Schema sync" below). Protocol version: `"1.0"`.

### Config file

```toml
port = 8765
token = "<64 hex chars>"
```

### Currently implemented frames

| Frame (brain -> mod) | Behaviour |
|---|---|
| `hello` (first frame only) | token check -> `hello_ack` or close 1008 |
| `request` `capabilities/list` | capability list + `protocol_version` |
| `request` any other method | `-32601` `not implemented: <method>` (until M1-C) |
| `task` (NEKO) | immediate `task_finished` `status=interrupted` `text="not implemented"`, `task_id` echoed verbatim (placeholder until M1-C) |
| invalid JSON | `-32700` parse error |
| non-object JSON / unknown frame type / malformed request | `-32600` invalid frame |

Response frames always carry `type/id/result/error` exactly as the frozen
schema (`sirius-brain/schema/frames/`) prescribes.

### Threading model

WebSocket callbacks run on Java-WebSocket's own threads. Parsing, validation
and dispatching happen there; any access to game state must be scheduled onto
the client main (render) thread - `ToolContext.onMainThread(Runnable)` wraps
`Minecraft.getInstance().execute(...)`. Writing frames back to the socket is
thread-safe from any thread. Handlers are registered once at server start-up
(`ToolRegistry`), so adding tools never touches the dispatcher.

### Schema sync (single source of truth)

`gradlew build` runs the `syncToolSchemas` task, which copies
`../sirius-brain/schema/index.json` + `tools/*.json` into the jar under
`schema/`. At runtime `capabilities/list` assembles its response from those
resources - the mod never re-declares tool names or schemas by hand, and the
sirius-brain repository is only ever read, never written.

## Build

```bash
gradlew build
```

The built jar lands in `build/libs/` with `Java-WebSocket-1.5.7.jar` embedded
under `META-INF/jarjar/` (loaded by NeoForge in production). Dev runs
(`gradlew runClient`) get the library via the `clientAdditionalRuntimeClasspath`
configuration instead.

## Deploy to test client

```bash
deploy.cmd
```

Runs `gradlew build` (which includes the schema sync from sirius-brain), then
copies `build/libs/sirius_bridge-*.jar` (excluding `-sources`/`-javadoc`) into
`..\.minecraft\versions\1.21.1-Sirius\mods\`, removing any older
`sirius_bridge-*.jar` there first, and prints the deployed jar name. Safe to
re-run (idempotent). The Gradle invocation inside includes local proxy flags
(`localhost:9674`) - remove them if the machine has direct internet access.

## Development

- `gradlew runClient` - launch a dev Minecraft client with the mod loaded
- `gradlew runServer` - launch a dev server (mod is client-only, does nothing there)
- `gradlew runData` - data generation

## Project layout

```
src/main/java/io/sirius/bridge/
    SiriusBridge.java     mod entry: lifecycle wiring (start on first tick, stop on shutdown)
    BridgeServer.java     WebSocket server: hello/token handshake, frame dispatch, audit
    BridgeConfig.java     config/sirius_bridge.toml (port + token, generates token on first run)
    AuditLog.java         logs/sirius_bridge.log (one line per security/protocol event)
    Capabilities.java     capability list assembled from the frozen schema resources
    ToolRegistry.java     method -> handler registry (add tools here, dispatcher untouched)
    ToolContext.java      per-call context: main-thread marshalling + thread-safe send
    Json.java             wire-frame builders + JSON-RPC style error codes
src/main/templates/META-INF/      neoforge.mods.toml template (properties expanded at build time)
build.gradle                      jarJar dependency + syncToolSchemas task
src/main/resources/assets/sirius_bridge/   Asset placeholder
```

## Next steps (per §8.2 of the technical spec)

- M1-C: real tool implementations - screenshot, getStats, world.query
- M2: input.* primitives (mouse/keyboard injection) + event subscription push
- Permission tiers (`observe`/`input_world`/`input_gui`) and input rate
  limiting (~20/s) are deliberately left for M2 (M1-B implements localhost
  binding, token handshake and the audit log).
