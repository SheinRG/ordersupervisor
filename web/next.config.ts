import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root: there are other lockfiles further up the tree
  // (sibling assignment projects), and without this Next infers the wrong one
  // and warns on every start.
  turbopack: {
    root: path.join(__dirname),
  },
};

export default nextConfig;
