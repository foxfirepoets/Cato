export interface ChatSocketPayload {
  type: "message" | "health";
  text?: string;
  session_id?: string;
}

export function buildChatMessagePayload(text: string, sessionId: string): ChatSocketPayload {
  return {
    type: "message",
    text,
    session_id: sessionId,
  };
}

export function sendChatSocketPayload(ws: WebSocket, payload: ChatSocketPayload): void {
  ws.send(JSON.stringify(payload));
}
