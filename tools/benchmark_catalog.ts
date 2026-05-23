import { performance } from "node:perf_hooks";
import { defaultPaths } from "../src/server/lib";
import { scanDataCatalog } from "../src/server/reconciliation";

const paths = defaultPaths();
const start = performance.now();
const catalog = await scanDataCatalog(paths);
const elapsedMs = Math.round(performance.now() - start);

console.log(JSON.stringify({
  elapsed_ms: elapsedMs,
  data_root: catalog.data_root,
  reports: catalog.reports.length,
  sources: catalog.sources.length,
  scanned_files: catalog.scanned_files,
  discovered_routes: catalog.discovered_routes.length,
  truncated: catalog.truncated,
  scan_roots: catalog.scan_roots.map((root) => ({
    kind: root.kind,
    exists: root.exists,
    files: root.files || 0
  }))
}, null, 2));
