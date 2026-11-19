export default function (omp) {
  const configuration = process.env.MUSE_GLIMMER_OMP_MODEL;
  if (!configuration) return;
  const { baseUrl, id, contextWindow } = JSON.parse(configuration);
  omp.registerProvider("muse-glimmer", {
    baseUrl,
    api: "openai-completions",
    apiKey: "muse-glimmer",
    models: [{
      id,
      name: "Muse Glimmer 30B",
      reasoning: true,
      // Muse always reasons; expose only the strengths the server accepts.
      thinking: {
        mode: "effort",
        efforts: ["low", "medium", "high"],
        requiresEffort: true,
      },
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow,
      maxTokens: 32768,
      compat: {
        supportsStore: false,
        supportsDeveloperRole: false,
        supportsReasoningEffort: true,
        supportsForcedToolChoice: false,
        maxTokensField: "max_tokens",
      },
    }],
  });

  // OMP does not send Pi's affinity headers for custom providers. These hooks
  // are rebound to each subagent, so each conversation gets its own KV session.
  omp.on("before_provider_request", (event, ctx) => {
    if (ctx.model?.provider !== "muse-glimmer") return;
    return { ...event.payload, session_id: ctx.sessionManager.getSessionId() };
  });
}
