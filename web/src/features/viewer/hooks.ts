// Browser conditions the viewer reacts to. Each falls back to a safe default where the API
// is missing (jsdom, old browsers), so the viewer still renders everything.
import { useEffect, useState, type RefObject } from "react";

function query(media: string): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(media)
    : null;
}

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => query("(prefers-reduced-motion: reduce)")?.matches ?? false,
  );
  useEffect(() => {
    const list = query("(prefers-reduced-motion: reduce)");
    if (!list) return;
    const onChange = () => setReduced(list.matches);
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/** Tracks window.devicePixelRatio, which changes with browser zoom or a monitor switch. */
export function useDevicePixelRatio(): number {
  const read = () => (typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1);
  const [ratio, setRatio] = useState(read);
  useEffect(() => {
    const list = query(`(resolution: ${ratio}dppx)`);
    if (!list) return;
    const onChange = () => setRatio(read());
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [ratio]);
  return ratio;
}

/**
 * True while the element is within about two screens of the scroll root's viewport, so
 * pages render shortly before they scroll in. Without IntersectionObserver every page counts
 * as near.
 */
export function useNearViewport(
  target: RefObject<Element | null>,
  root: RefObject<Element | null>,
): boolean {
  const supported = typeof IntersectionObserver !== "undefined";
  const [near, setNear] = useState(!supported);
  useEffect(() => {
    const element = target.current;
    if (!supported || !element) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[entries.length - 1];
        if (entry) setNear(entry.isIntersecting);
      },
      { root: root.current, rootMargin: "150% 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [target, root, supported]);
  return near;
}
