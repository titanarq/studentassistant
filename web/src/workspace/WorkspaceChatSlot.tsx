import { useCallback } from "react";
import EditorChat, { type OpenSource } from "../chat/EditorChat";
import { useEditorChat } from "../chat/useEditorChat";
import { useWorkspace } from "./state";

/**
 * The chat slot of the workspace (#312): for now the existing editor chat, unchanged, whose
 * applied turns refresh the document through `reloadNotes()`. #317 replaces this component with
 * the live chat panel; the page only renders `<WorkspaceChatSlot onOpenSource />`.
 */
export default function WorkspaceChatSlot({ onOpenSource }: { onOpenSource: OpenSource }) {
  const { subjectId, topicId, reloadNotes } = useWorkspace();
  const onNotesChanged = useCallback((sections: string[]) => void reloadNotes(sections), [reloadNotes]);
  const chat = useEditorChat(subjectId, topicId, onNotesChanged);
  return <EditorChat chat={chat} onOpenSource={onOpenSource} />;
}
