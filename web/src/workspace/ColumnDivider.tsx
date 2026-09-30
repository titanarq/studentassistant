import { type KeyboardEvent, type PointerEvent, type RefObject, useRef } from "react";
import { clampSide, MAX_SIDE_PERCENT, MIN_SIDE_PERCENT } from "./layoutState";

const STEP = 2;
const BIG_STEP = 10;

/**
 * The draggable divider between the chat column (left) and the document (#534): a vertical
 * `separator` the pointer drags and the arrow keys move (Shift: bigger steps; Home/End: the
 * limits). `value` is the chat column's share of the width in percent; `onChange` gets a value
 * already clamped to the sane range, `onCommit` the final one (when the drag ends or a key is
 * pressed) so the caller can store it.
 */
export default function ColumnDivider({
  value,
  columns,
  onChange,
  onCommit,
}: {
  value: number;
  /** The grid the divider sits in, to turn the pointer's position into a share. */
  columns: RefObject<HTMLElement | null>;
  onChange: (percent: number) => void;
  onCommit: (percent: number) => void;
}) {
  const dragging = useRef(false);
  const last = useRef(value);
  last.current = value;

  const fromPointer = (event: PointerEvent<HTMLElement>): number | null => {
    const grid = columns.current;
    if (grid === null) return null;
    const box = grid.getBoundingClientRect();
    if (box.width <= 0) return null;
    const half = event.currentTarget.getBoundingClientRect().width / 2;
    return clampSide(((event.clientX - box.left - half) / box.width) * 100);
  };

  const onPointerDown = (event: PointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    dragging.current = true;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  };

  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    if (!dragging.current) return;
    const next = fromPointer(event);
    if (next === null) return;
    last.current = next;
    onChange(next);
  };

  const end = (event: PointerEvent<HTMLElement>) => {
    if (!dragging.current) return;
    dragging.current = false;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    onCommit(last.current);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const step = event.shiftKey ? BIG_STEP : STEP;
    let next: number;
    if (event.key === "ArrowRight") next = value + step;
    else if (event.key === "ArrowLeft") next = value - step;
    else if (event.key === "Home") next = MIN_SIDE_PERCENT;
    else if (event.key === "End") next = MAX_SIDE_PERCENT;
    else return;
    event.preventDefault();
    next = clampSide(next);
    onChange(next);
    onCommit(next);
  };

  return (
    <div
      className="workspace-divider"
      role="separator"
      aria-orientation="vertical"
      aria-label="Ancho del chat"
      aria-valuemin={MIN_SIDE_PERCENT}
      aria-valuemax={MAX_SIDE_PERCENT}
      aria-valuenow={Math.round(value)}
      aria-valuetext={`${Math.round(value)} % de la pantalla`}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onKeyDown={onKeyDown}
    />
  );
}
