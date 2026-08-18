package io.sirius.bridge;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;

/**
 * In-process smoke test for the M1-C perception tools (run via
 * {@code gradlew smokeTest}). No game, no client: it exercises the pure
 * halves - parameter validation, bbox cropping, the JPEG budget ladder,
 * response assembly, block scanning and entity filtering - exactly the logic
 * that would otherwise only be verifiable inside a running Minecraft.
 *
 * <p>Exit code 0 = all checks passed; any failure prints the check name and
 * exits 1.
 */
public final class SmokeMain {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        screenshotParams();
        worldQueryParams();
        imageOps();
        contracts();

        System.out.println();
        System.out.println("smoke: " + passed + " passed, " + failures.size() + " failed");
        if (!failures.isEmpty()) {
            failures.forEach(f -> System.out.println("  FAILED: " + f));
            System.exit(1);
        }
        System.out.println("smoke: OK");
    }

    // ------------------------------------------------------------------ checks

    private static void screenshotParams() throws Exception {
        ToolContracts.ScreenshotParams p;

        p = validParams(() -> ToolContracts.screenshotParams(json("{\"tier\":\"full\"}")));
        check("full".equals(p.tier()) && p.bbox() == null && p.quality() == 80,
                "screenshot: full defaults (bbox null, quality 80)");

        p = validParams(() -> ToolContracts.screenshotParams(
                json("{\"tier\":\"crop\",\"bbox\":[10,20,300,400],\"quality\":90}")));
        check(Arrays.equals(p.bbox(), new int[]{10, 20, 300, 400}) && p.quality() == 90,
                "screenshot: crop + bbox + quality parsed");

        p = validParams(() -> ToolContracts.screenshotParams(
                json("{\"tier\":\"crop\",\"bbox\":[10.4,20.6,100.9,50.2],\"quality\":null}")));
        check(Arrays.equals(p.bbox(), new int[]{10, 21, 101, 50}) && p.quality() == 80,
                "screenshot: float bbox rounds, null quality defaults");

        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"crop\"}")),
                "screenshot: crop without bbox rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{}")),
                "screenshot: missing tier rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"huge\"}")),
                "screenshot: bad tier enum rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"crop\",\"bbox\":[1,2,3]}")),
                "screenshot: short bbox rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"crop\",\"bbox\":[\"a\",2,3,4]}")),
                "screenshot: non-numeric bbox rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"crop\",\"bbox\":[0,0,0,10]}")),
                "screenshot: zero-width bbox rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"full\",\"quality\":101}")),
                "screenshot: quality 101 rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"full\",\"quality\":80.5}")),
                "screenshot: fractional quality rejected");
        expectInvalid(() -> ToolContracts.screenshotParams(json("{\"tier\":\"full\",\"quality\":\"80\"}")),
                "screenshot: string quality rejected");
    }

    private static void worldQueryParams() throws Exception {
        ToolContracts.WorldQueryParams p;

        p = validParams(() -> ToolContracts.worldQueryParams(json("{\"type\":\"blocks\",\"range\":16}")));
        check("blocks".equals(p.type()) && p.range() == 16.0, "world.query: blocks range 16 parsed");

        p = validParams(() -> ToolContracts.worldQueryParams(json("{\"type\":\"entities\",\"range\":0.5}")));
        check(p.range() == 0.5, "world.query: fractional range accepted");

        p = validParams(() -> ToolContracts.worldQueryParams(json("{\"type\":\"blocks\",\"range\":64}")));
        check(p.range() == 64.0, "world.query: range boundary 64 accepted");

        p = validParams(() -> ToolContracts.worldQueryParams(json("{\"type\":\"entities\"}")));
        check(p.range() == 16.0, "world.query: missing range defaults to 16");

        expectInvalid(() -> ToolContracts.worldQueryParams(json("{\"range\":16}")),
                "world.query: missing type rejected");
        expectInvalid(() -> ToolContracts.worldQueryParams(json("{\"type\":\"chunks\",\"range\":8}")),
                "world.query: bad type enum rejected");
        expectInvalid(() -> ToolContracts.worldQueryParams(json("{\"type\":\"blocks\",\"range\":0}")),
                "world.query: zero range rejected");
        expectInvalid(() -> ToolContracts.worldQueryParams(json("{\"type\":\"blocks\",\"range\":-3}")),
                "world.query: negative range rejected");
        expectInvalid(() -> ToolContracts.worldQueryParams(json("{\"type\":\"blocks\",\"range\":64.5}")),
                "world.query: range over cap rejected");
        expectInvalid(() -> ToolContracts.worldQueryParams(json("{\"type\":\"blocks\",\"range\":\"16\"}")),
                "world.query: string range rejected");
    }

    private static void imageOps() throws Exception {
        // --- crop
        BufferedImage base = new BufferedImage(100, 60, BufferedImage.TYPE_INT_RGB);
        check(ImageOps.crop(base, new int[]{10, 10, 50, 20}).getWidth() == 50
                && ImageOps.crop(base, new int[]{10, 10, 50, 20}).getHeight() == 20,
                "image: exact crop is 50x20");
        BufferedImage clamped = ImageOps.crop(base, new int[]{90, 50, 100, 100});
        check(clamped.getWidth() == 10 && clamped.getHeight() == 10, "image: bbox clamped to image bounds");
        boolean threw = false;
        try {
            ImageOps.crop(base, new int[]{200, 200, 10, 10});
        } catch (IOException expected) {
            threw = true;
        }
        check(threw, "image: non-intersecting bbox throws");

        // --- JPEG round trip
        BufferedImage gradient = new BufferedImage(64, 48, BufferedImage.TYPE_INT_RGB);
        for (int y = 0; y < 48; y++) {
            for (int x = 0; x < 64; x++) {
                gradient.setRGB(x, y, (x * 4) << 16 | (y * 5) << 8 | 128);
            }
        }
        byte[] jpeg = ImageOps.encodeJpeg(gradient, 80);
        BufferedImage decoded = ImageIO.read(new ByteArrayInputStream(jpeg));
        check(decoded != null && decoded.getWidth() == 64 && decoded.getHeight() == 48,
                "image: JPEG decodable with matching dimensions");
        check(ImageOps.base64Length(jpeg) == ImageOps.base64(jpeg).length(),
                "image: base64 length predictor matches actual");

        // --- quality ladder
        check(Arrays.equals(ImageOps.qualityLadder(80), new int[]{80, 70, 60, 50, 40}),
                "image: ladder 80 -> [80,70,60,50,40]");
        check(Arrays.equals(ImageOps.qualityLadder(45), new int[]{45, 40}), "image: ladder 45 -> [45,40]");
        check(Arrays.equals(ImageOps.qualityLadder(30), new int[]{30}), "image: ladder 30 stays at 30");
        check(Arrays.equals(ImageOps.qualityLadder(40), new int[]{40}), "image: ladder 40 stays at 40");

        // --- scale
        BufferedImage wide = new BufferedImage(2048, 1024, BufferedImage.TYPE_INT_RGB);
        BufferedImage scaled = ImageOps.scaleLongestEdge(wide, 1024);
        check(scaled.getWidth() == 1024 && scaled.getHeight() == 512, "image: longest edge scaled to 1024");
        check(ImageOps.scaleLongestEdge(scaled, 1024) == scaled, "image: small image not rescaled");

        // --- budget: compressible image stays at full size
        ImageOps.Encoded flat = ImageOps.encodeWithinBudget(gradient, 80);
        check(!flat.downscaled() && flat.base64Length() <= ImageOps.MAX_BASE64_LENGTH,
                "image: small image within budget without downscale");

        // --- budget: 4K incompressible noise must degrade (4K noise at q40 still
        // overruns 2MB of base64, so the ladder has to reach the 1024px scale).
        BufferedImage noise = new BufferedImage(3840, 2160, BufferedImage.TYPE_INT_RGB);
        java.util.Random random = new Random(42);
        int[] row = new int[3840];
        for (int y = 0; y < 2160; y++) {
            for (int x = 0; x < 3840; x++) {
                row[x] = random.nextInt(0x1000000);
            }
            noise.setRGB(0, y, 3840, 1, row, 0, 3840);
        }
        ImageOps.Encoded huge = ImageOps.encodeWithinBudget(noise, 80);
        BufferedImage hugeDecoded = ImageIO.read(new ByteArrayInputStream(huge.jpeg()));
        check(huge.base64Length() <= ImageOps.MAX_BASE64_LENGTH,
                "image: 4K noise ends within budget (" + huge.base64Length() + " b64 chars)");
        check(huge.downscaled() && Math.max(hugeDecoded.getWidth(), hugeDecoded.getHeight()) <= 1024,
                "image: 4K noise downscaled to <=1024 longest edge ("
                        + hugeDecoded.getWidth() + "x" + hugeDecoded.getHeight() + ")");
    }

    private static void contracts() {
        // --- notInGame
        check(!ToolContracts.notInGame().get("in_game").getAsBoolean(), "contracts: notInGame shape");

        // --- screenshotResult
        JsonObject shot = ToolContracts.screenshotResult("QUJD", 64, 48, 1724000000000L, 70, false);
        check("QUJD".equals(shot.get("image_b64").getAsString())
                && "jpeg".equals(shot.get("format").getAsString())
                && shot.get("width").getAsInt() == 64
                && shot.get("height").getAsInt() == 48
                && shot.get("taken_at").getAsLong() == 1724000000000L
                && shot.get("quality").getAsInt() == 70
                && !shot.get("downscaled").getAsBoolean(),
                "contracts: screenshotResult fields");

        // --- statsResult
        ToolContracts.StatsSnapshot stats = new ToolContracts.StatsSnapshot(
                18.5f, 17, 4.2f, 300, 27, 0.6f,
                1.5, 64.0, -12.25, "minecraft:overworld", "survival",
                List.of(new ToolContracts.EffectFact("minecraft:speed", 1200, 1)), true);
        JsonObject statsJson = ToolContracts.statsResult(stats);
        check(statsJson.get("in_game").getAsBoolean()
                && statsJson.get("health").getAsFloat() == 18.5f
                && statsJson.get("food").getAsInt() == 17
                && statsJson.get("saturation").getAsFloat() == 4.2f
                && statsJson.get("air").getAsInt() == 300
                && statsJson.get("xp_level").getAsInt() == 27
                && statsJson.get("xp_progress").getAsFloat() == 0.6f
                && statsJson.get("position").getAsJsonObject().get("y").getAsDouble() == 64.0
                && "minecraft:overworld".equals(statsJson.get("dimension").getAsString())
                && "survival".equals(statsJson.get("game_mode").getAsString())
                && statsJson.get("effects").getAsJsonArray().size() == 1
                && statsJson.get("effects").getAsJsonArray().get(0).getAsJsonObject()
                        .get("id").getAsString().equals("minecraft:speed")
                && statsJson.get("alive").getAsBoolean(),
                "contracts: statsResult full shape");

        // --- scanBlocks: solid 3x3x3 with one air hole, range 1
        JsonObject small = ToolContracts.scanBlocks(0, 0, 0, 1,
                (x, y, z) -> (x == 0 && y == 1 && z == 0) ? null : "minecraft:stone");
        check(small.get("count").getAsInt() == 26 && !small.get("truncated").getAsBoolean()
                        && small.get("blocks").getAsJsonArray().size() == 26
                        && "minecraft:stone".equals(small.get("blocks").getAsJsonArray().get(0)
                                .getAsJsonObject().get("block").getAsString()),
                "contracts: scanBlocks counts non-air, skips air");

        // --- scanBlocks: 11x11x11 solid -> truncated at 512
        JsonObject big = ToolContracts.scanBlocks(0, 0, 0, 5, (x, y, z) -> "minecraft:dirt");
        check(big.get("count").getAsInt() == ToolContracts.BLOCKS_CAP
                        && big.get("truncated").getAsBoolean(),
                "contracts: scanBlocks truncates at " + ToolContracts.BLOCKS_CAP);

        // --- scanBlocks: range 0.5 -> radius 1 cube (27 blocks max)
        JsonObject tiny = ToolContracts.scanBlocks(0, 0, 0, 0.5, (x, y, z) -> "minecraft:stone");
        check(tiny.get("count").getAsInt() == 27, "contracts: fractional range uses ceil radius");

        // --- filterEntities: distance + health omission + cap
        List<ToolContracts.EntityFact> facts = List.of(
                new ToolContracts.EntityFact("u-self", "Steve", "minecraft:player", 0, 64, 0, 20f),
                new ToolContracts.EntityFact("u-near", "Zombie", "minecraft:zombie", 5, 64, 0, 12f),
                new ToolContracts.EntityFact("u-far", "Zombie", "minecraft:zombie", 10, 64, 0, 12f),
                new ToolContracts.EntityFact("u-item", "Diamond", "minecraft:diamond", 2, 64, 0, Float.NaN));
        JsonObject entities = ToolContracts.filterEntities(facts, 0, 64, 0, 8);
        check(entities.get("count").getAsInt() == 3
                        && entities.get("entities").getAsJsonArray().size() == 3,
                "contracts: entities filtered by range");
        JsonElement item = entities.get("entities").getAsJsonArray().get(2);
        check(!item.getAsJsonObject().has("health"),
                "contracts: NaN health omitted from entity entry");

        List<ToolContracts.EntityFact> crowd = new ArrayList<>();
        for (int i = 0; i < 200; i++) {
            crowd.add(new ToolContracts.EntityFact("u" + i, "E" + i, "minecraft:zombie", i % 4, 64, 0, 1f));
        }
        check(ToolContracts.filterEntities(crowd, 0, 64, 0, 16).get("count").getAsInt()
                        == ToolContracts.ENTITIES_CAP,
                "contracts: entities capped at " + ToolContracts.ENTITIES_CAP);
    }

    // ------------------------------------------------------------------ helpers

    private static JsonObject json(String text) {
        return JsonParser.parseString(text).getAsJsonObject();
    }

    private static <T> T validParams(Supplier_<T> parse) throws Exception {
        return parse.get();
    }

    private static void expectInvalid(Supplier_<?> parse, String name) {
        try {
            parse.get();
            check(false, name + " (no exception thrown)");
        } catch (ToolContracts.InvalidParams expected) {
            check(true, name);
        } catch (Exception other) {
            check(false, name + " (wrong exception: " + other + ")");
        }
    }

    @FunctionalInterface
    private interface Supplier_<T> {
        T get() throws Exception;
    }

    private static void check(boolean condition, String name) {
        if (condition) {
            passed++;
            System.out.println("  ok    " + name);
        } else {
            failures.add(name);
            System.out.println("  FAIL  " + name);
        }
    }
}
