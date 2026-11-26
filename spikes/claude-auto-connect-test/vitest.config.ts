import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.toml" },
      miniflare: {
        // Test-only values; the real ones are deploy-time vars and a wrangler secret.
        bindings: {
          CLAUDE_ISSUER: "https://identity.anthropic.com/issuers/test-beta-issuer",
          GOOGLE_CLIENT_ID: "test-google-client-id.apps.googleusercontent.com",
          GOOGLE_CLIENT_SECRET: "test-google-client-secret",
        },
      },
    }),
  ],
});
