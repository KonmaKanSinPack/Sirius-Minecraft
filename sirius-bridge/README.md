# sirius-bridge

NeoForge mod running on the **real Minecraft client**, acting as the "eyes and hands" of the Sirius AI brain: screenshot capture, input injection, and event push. See `../sirius-technical.md` §8.2 for the full spec.

> Current status: **project skeleton only** - minimal mod entry point, no functionality yet.

## Versions

| Component | Version |
|---|---|
| Minecraft | 1.21.1 |
| NeoForge | 21.1.233 (1.21.1 line = 21.1.x) |
| ModDevGradle | 2.0.141 |
| Gradle wrapper | 9.2.0 |
| Java | 21 (JDK 21 required) |

## Build

```bash
gradlew build
```

The built jar lands in `build/libs/`.

## Development

- `gradlew runClient` - launch a dev Minecraft client with the mod loaded
- `gradlew runServer` - launch a dev server
- `gradlew runData` - data generation

## Project layout

```
src/main/java/io/sirius/bridge/   Mod code (currently only SiriusBridge.java)
src/main/templates/META-INF/      neoforge.mods.toml template (properties expanded at build time)
src/main/resources/assets/sirius_bridge/   Asset placeholder
```

## Next steps (per §8.2 of the technical spec)

- Screenshot capture of the game window / framebuffer
- Input injection (mouse + keyboard)
- Game event push to the Sirius AI brain over a local socket/HTTP bridge
- Health/inventory/state polling endpoints
