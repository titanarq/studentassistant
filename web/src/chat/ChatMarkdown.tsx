import ReplyView from "../study/chat/ReplyView";
import "./chat.css";

/**
 * A chat reply written in light Markdown (paragraphs, **bold**, *italic*, lists, `code`,
 * blockquotes) drawn through the notes' own parser, so the text of a reply never reaches the DOM as
 * markup (no raw HTML), links open with `noopener noreferrer` and only http(s)/mailto ones are live.
 * A footnote mark `[^p4]` stays visible as `[p4]`; a half-written Markdown mark while the reply
 * streams simply shows as text until it closes.
 */
export default function ChatMarkdown({ text }: { text: string }) {
  return (
    <ReplyView
      text={text}
      className="chat-md"
      renderSection={(anchor, key) => <span key={key}>[§{anchor}]</span>}
      renderSource={(label, key) => (
        <span key={key} className="chat-fnref">
          [{label}]
        </span>
      )}
    />
  );
}
