// Activate only for muse-glimmer launches and their child Pi processes.
export default function (pi) {
  const configuration = process.env.MUSE_GLIMMER_PI_MODEL;
  if (!configuration) return;
  const { baseUrl, id, contextWindow } = JSON.parse(configuration);
  pi.registerProvider("muse-glimmer", {
    baseUrl,
    api: "openai-completions",
    apiKey: "muse-glimmer",
    models: [{
      id,
      name: "Muse Glimmer 30B",
      reasoning: true,
      // Muse always reasons; expose only the strengths the server accepts.
      thinkingLevelMap: {
        off: null,
        minimal: null,
        low: "low",
        medium: "medium",
        high: "high",
        xhigh: null,
        max: null,
      },
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow,
      maxTokens: 32768,
      compat: {
        supportsStore: false,
        // The Muse template only renders system messages, not developer messages.
        supportsDeveloperRole: false,
        supportsReasoningEffort: true,
        supportsLongCacheRetention: false,
        maxTokensField: "max_tokens",
        sendSessionAffinityHeaders: true,
        sessionAffinityFormat: "openai",
      },
    }],
  });
}
