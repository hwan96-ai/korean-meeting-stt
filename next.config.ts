import type { NextConfig } from "next";
import { resolve } from "node:path";
const runtime = resolve(process.cwd(), "lib/runtime-node.ts");
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  webpack(config, { webpack }) {
    config.resolve.alias["@/lib/runtime"] = runtime;
    // Next's tsconfig resolver precedes aliases. Replace this exact boundary
    // before resolution so a Node bundle can never import cloudflare:workers.
    config.plugins.push(new webpack.NormalModuleReplacementPlugin(/^@\/lib\/runtime$/, (resource: {request: string}) => { resource.request = runtime; }));
    return config;
  },
  turbopack: { resolveAlias: { "@/lib/runtime": "./lib/runtime-node.ts" } },
};
export default config;
