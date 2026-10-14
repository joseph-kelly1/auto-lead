import { access, copyFile, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = fileURLToPath(new URL("..", import.meta.url));
const publicDir = join(appRoot, "public");
const artifactDir = join(appRoot, "..", "pipeline", "artifacts");
const files = ["parcels.parquet", "parcels.pmtiles"];

if (process.argv.includes("--check")) {
  for (const file of files) await access(join(publicDir, file));
  const dataset = JSON.parse(await readFile(join(publicDir, "dataset.json"), "utf8"));
  if (dataset.kind !== "synthetic" && dataset.kind !== "private")
    throw new Error("Unknown dataset kind. Generate the synthetic demo or stage a private build.");
  if (dataset.kind === "private" && process.env.ALLOW_PRIVATE_ARTIFACTS !== "1")
    throw new Error("Private data cannot be built for publication by default. Restore the synthetic demo.");
  if (dataset.kind === "synthetic") {
    for (const file of files) {
      const hash = createHash("sha256").update(await readFile(join(publicDir, file))).digest("hex");
      if (dataset.sha256?.[file] !== hash)
        throw new Error(`Demo checksum mismatch for ${file}. Regenerate the synthetic demo.`);
    }
  }
  console.log(`Validated ${dataset.kind} parcel artifacts.`);
} else {
  if (!process.argv.includes("--private"))
    throw new Error(
      "Private data requires explicit staging: npm run stage -- --private. " +
      "This replaces the public demo with real property records. Do not commit the result.",
    );
  // Validate both sources before replacing either member of the artifact pair.
  for (const file of files) await access(join(artifactDir, file));
  for (const file of files)
    await copyFile(join(artifactDir, file), join(publicDir, file));
  await writeFile(join(publicDir, "dataset.json"), JSON.stringify({ kind: "private" }) + "\n");
  console.log("Staged private artifacts. Restore the synthetic demo before committing.");
}
