import { type KeyboardEvent, type ReactNode, useRef } from "react";

export interface WorkspaceTab<K extends string> {
  key: K;
  label: ReactNode;
  panel: ReactNode;
}

/**
 * An accessible tab list (WAI-ARIA tabs, automatic activation): Left/Right move between tabs
 * with wrap-around, Home/End go to the first/last. Every panel stays mounted; an inactive one is
 * only `hidden`, so a running capture keeps its camera, recognizer and socket.
 */
export default function WorkspaceTabs<K extends string>({
  id,
  label,
  tabs,
  active,
  onChange,
}: {
  id: string;
  label: string;
  tabs: WorkspaceTab<K>[];
  active: K;
  onChange: (key: K) => void;
}) {
  const refs = useRef(new Map<K, HTMLButtonElement>());

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const index = tabs.findIndex((tab) => tab.key === active);
    let next: number;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    const key = tabs[next].key;
    onChange(key);
    refs.current.get(key)?.focus();
  };

  return (
    <div className="workspace-tabs">
      <div role="tablist" aria-label={label} className="workspace-tablist">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            ref={(element) => {
              if (element) refs.current.set(tab.key, element);
              else refs.current.delete(tab.key);
            }}
            type="button"
            role="tab"
            id={`${id}-tab-${tab.key}`}
            aria-controls={`${id}-panel-${tab.key}`}
            aria-selected={tab.key === active}
            tabIndex={tab.key === active ? 0 : -1}
            className="workspace-tab"
            onClick={() => onChange(tab.key)}
            onKeyDown={onKeyDown}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.key}
          role="tabpanel"
          id={`${id}-panel-${tab.key}`}
          aria-labelledby={`${id}-tab-${tab.key}`}
          hidden={tab.key !== active}
          className="workspace-tabpanel"
        >
          {tab.panel}
        </div>
      ))}
    </div>
  );
}
