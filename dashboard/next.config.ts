import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // better-sqlite3 is a native module. Mark it external so Next.js doesn't
  // try to bundle it for server components / API routes.
  serverExternalPackages: ["better-sqlite3"],

  async headers() {
    return [
      {
        // Long-cache email banner assets so Gmail's image proxy and any
        // recipient's local cache hold them for a year. The filename is
        // the cache key — change the filename when the banner content
        // changes (or version it like ostium-banner-v2.png).
        source: "/email-assets/:path*",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=31536000, immutable",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
