export default {
  id: "muse-glimmer",
  async setup(ctx) {
    await ctx.session.hook("model.request", (event) => {
      // OpenCode's cache-affinity headers group parents and children together.
      // Stateful server sessions must also separate titles and compaction calls.
      event.headers.session_id = `${event.sessionID}:${event.kind}`;
    }, { providerID: "muse-glimmer" });
  },
};
