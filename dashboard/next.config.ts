import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // better-sqlite3 is a native module. Mark it external so Next.js doesn't
  // try to bundle it for server components / API routes.
  serverExternalPackages: ["better-sqlite3"],
};

export default nextConfig;
