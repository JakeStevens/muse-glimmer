import { streamSimple } from "@oh-my-pi/pi-ai";

export default function (omp) {
  const configuration = process.env.MUSE_GLIMMER_OMP_MODEL;
  if (!configuration) return;
  const { baseUrl, id, contextWindow } = JSON.parse(configuration);
  omp.registerProvider("muse-glimmer", {
    baseUrl,
    api: "muse-glimmer-openai",
    apiKey: "muse-glimmer",
    streamSimple(model, context, options) {
      // Side turns and subagents have their own provider IDs, unlike the UI session.
      const headers = { ...options?.headers };
      if (options?.sessionId) headers.session_id = options.sessionId;
      return streamSimple({ ...model, api: "openai-completions" }, context, { ...options, headers });
    },
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
}
