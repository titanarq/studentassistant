import { type RefObject, useCallback, useLayoutEffect, useRef, useState } from "react";

/** How close to the end (px) still counts as "at the newest turn". */
export const NEAR_END_PX = 48;

export interface FollowLog<T extends HTMLElement> {
  /** The log's own scroll area. */
  ref: RefObject<T | null>;
  /** On the log's `onScroll`: whether the student left the end (or came back to it). */
  onScroll: () => void;
  /** Something new arrived while the student had scrolled up: show «Nuevos mensajes ↓». */
  unseen: boolean;
  /** Scroll to the newest turn and follow it again (the button, or the student sending). */
  follow: () => void;
}

function atEnd(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= NEAR_END_PX;
}

function toEnd(element: HTMLElement) {
  element.scrollTop = element.scrollHeight;
}

/**
 * A chat log that follows its newest turn (#412): whenever `content` changes (a turn added, a
 * streamed reply grown), the log scrolls to its end, unless the student has scrolled up to read
 * something older; then `unseen` is set until they come back to the end or call `follow()`.
 */
export function useFollowLog<T extends HTMLElement>(content: unknown): FollowLog<T> {
  const ref = useRef<T | null>(null);
  const following = useRef(true);
  const [unseen, setUnseen] = useState(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (element === null) return;
    if (following.current) toEnd(element);
    else setUnseen(true);
  }, [content]);

  const onScroll = useCallback(() => {
    const element = ref.current;
    if (element === null) return;
    following.current = atEnd(element);
    if (following.current) setUnseen(false);
  }, []);

  const follow = useCallback(() => {
    following.current = true;
    setUnseen(false);
    if (ref.current !== null) toEnd(ref.current);
  }, []);

  return { ref, onScroll, unseen, follow };
}
