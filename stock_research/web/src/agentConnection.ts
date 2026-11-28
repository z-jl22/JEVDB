import type { AgentConnection } from "./api";

export const LAST_CONNECTION_KEY = "jevdb.agent.lastConnection";

export function readLastConnection(): AgentConnection | null {
  try {
    const data = JSON.parse(localStorage.getItem(LAST_CONNECTION_KEY) ?? "null") as Partial<AgentConnection> | null;
    if (typeof data?.base_url === "string" && data.base_url && typeof data.model === "string" && data.model) {
      return { base_url: data.base_url, model: data.model };
    }
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
  return null;
}
