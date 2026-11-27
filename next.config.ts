import type { NextConfig } from "next";
import { resolve } from "node:path";
const runtime = resolve(process.cwd(), "lib/runtime-node.ts");
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // Nothing in this app is meant to be framed; this also stops clickjacking of the signed-in owner.
  // Only the OCI/local build (build:oci sets MEETING_RUNTIME=oci); the recovery Sites build keeps its host's framing.
  async headers() {
    if (process.env.MEETING_RUNTIME !== "oci") return [];
    // API responses (audio, JSON, TXT) render nothing, so they also keep default-src 'none'. Next sends the config
    // header instead of a route's own CSP, so the API rule must carry the whole policy.
    return [
      { source: "/:path*", headers: [{ key: "X-Frame-Options", value: "DENY" }, { key: "Content-Security-Policy", value: "frame-ancestors 'none'" }] },
      { source: "/api/:path*", headers: [{ key: "Content-Security-Policy", value: "default-src 'none'; frame-ancestors 'none'" }] },
    ];
  },
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
