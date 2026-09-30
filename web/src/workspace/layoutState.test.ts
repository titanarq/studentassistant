import { describe, expect, it } from "vitest";
import { type LayoutState, panelState, toggleChat, togglePanel } from "./layoutState";

// #538: the state machine of the chat and the left card, every combination.
const start: LayoutState = { panelCollapsed: false, chatExpanded: false };

describe("layout state", () => {
  it("names what the left card shows in each combination", () => {
    expect(panelState({ panelCollapsed: false, chatExpanded: false })).toBe("expanded");
    expect(panelState({ panelCollapsed: true, chatExpanded: false })).toBe("collapsed");
    expect(panelState({ panelCollapsed: false, chatExpanded: true })).toBe("hidden");
    // A collapsed pill stays visible above the maximized chat.
    expect(panelState({ panelCollapsed: true, chatExpanded: true })).toBe("collapsed");
  });

  it("expanding the pill while the chat is maximized reduces the chat", () => {
    expect(togglePanel({ panelCollapsed: true, chatExpanded: true })).toEqual({
      panelCollapsed: false,
      chatExpanded: false,
    });
  });

  it("expanding the chat hides a full card, keeps a collapsed one, and reducing it restores the previous state", () => {
    for (const panelCollapsed of [false, true]) {
      const before = { panelCollapsed, chatExpanded: false };
      const during = toggleChat(before);
      expect(panelState(during)).toBe(panelCollapsed ? "collapsed" : "hidden");
      const after = toggleChat(during);
      expect(after).toEqual(before);
      expect(panelState(after)).toBe(panelCollapsed ? "collapsed" : "expanded");
    }
  });

  it("the card's toggle flips its state and does nothing while the chat hides it", () => {
    const collapsed = togglePanel(start);
    expect(panelState(collapsed)).toBe("collapsed");
    expect(panelState(togglePanel(collapsed))).toBe("expanded");
    const hidden = toggleChat(start);
    expect(togglePanel(hidden)).toBe(hidden);
  });

  it("any sequence of the two toggles ends in a coherent state", () => {
    let state = start;
    for (const step of [togglePanel, toggleChat, toggleChat, togglePanel, toggleChat, togglePanel, toggleChat]) {
      state = step(state);
      expect(["expanded", "collapsed", "hidden"]).toContain(panelState(state));
    }
  });
});
