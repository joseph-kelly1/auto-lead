import { access, copyFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const publicDir = join(appRoot, "public");
const artifactDir = join(appRoot, "..", "artifacts");
const files = ["parcels.parquet", "parcels.pmtiles"];

await mkdir(publicDir, { recursive: true });

for (const file of files) {
  const source = join(artifactDir, file);
  const destination = join(publicDir, file);

  try {
    await copyFile(source, destination);
    console.log(`Staged ${file} from ../artifacts.`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;

    try {
      await access(destination);
      console.log(`Using checked-in public/${file}.`);
    } catch {
      throw new Error(
        `Missing ${file}. Generate ../artifacts/${file} locally and run npm run stage before committing.`,
      );
    }
  }
}
