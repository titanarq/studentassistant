import { useCallback } from "react";
import type { OpenSource } from "../chat/EditorChat";
import { topicPath } from "../desk/api";
import ChatPanel from "./chat/ChatPanel";
import { useWorkspaceChat } from "./chat/useWorkspaceChat";
import { useWorkspace } from "./state";

/**
 * The chat slot of the workspace: the live chat panel (#317), fed by the topic's workspace stream;
 * every change of the notes it hears of reloads the document through `reloadNotes()`. The page
 * only renders `<WorkspaceChatSlot onOpenSource />` (the notes page keeps `EditorChat`).
 */
export default function WorkspaceChatSlot({
  onOpenSource,
  retryDelays,
}: {
  onOpenSource: OpenSource;
  /** The stream's reconnect backoff; tests pass short ones. */
  retryDelays?: readonly number[];
}) {
  const { subjectId, topicId, reloadNotes } = useWorkspace();
  const reload = useCallback((sections?: string[]) => void reloadNotes(sections), [reloadNotes]);
  const chat = useWorkspaceChat({ subjectId, topicId, reloadNotes: reload, retryDelays });
  return <ChatPanel chat={chat} versionsPath={`${topicPath(subjectId, topicId)}/versions`} onOpenSource={onOpenSource} />;
}
