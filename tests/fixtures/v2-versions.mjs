import { readFileSync } from "node:fs"

export const TESTED_V2_HOST_VERSIONS = Object.freeze(JSON.parse(readFileSync(new URL("./v2-sources.json", import.meta.url))).map(fixture => fixture.version))
