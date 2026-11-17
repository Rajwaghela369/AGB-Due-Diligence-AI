import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Review } from "../../api/types.gen";
import "../../i18n";
import { formatDuration, formatTokens } from "./format";
import { ReviewCost } from "./review-cost";

afterEach(cleanup);

describe("ReviewCost", () => {
  it("shows review money, elapsed time and cache share from the response", () => {
    render(
      <ReviewCost
        review={{ cost_usd: 1.5 } as Review}
        pricing={{ confirmed: true, eur_per_usd: 0.9 }}
        costEur={1.35}
        wallTimeMs={2500}
        cacheHitRate={0.25}
      />,
    );

    expect(screen.getByText((_, element) => element?.textContent === "1,35 €")).toBeInTheDocument();
    expect(screen.getByText("2,5 s")).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.textContent === "25 %")).toBeInTheDocument();
  });

  it("does not round a cache share below 100 % up to 100 %", () => {
    render(
      <ReviewCost
        review={{ cost_usd: 1 } as Review}
        pricing={undefined}
        costEur={null}
        wallTimeMs={1000}
        cacheHitRate={0.996}
      />,
    );
    expect(screen.getByTitle("Cache-Treffer").textContent).toMatch(/^99\s%$/);
  });
});

describe("format helpers", () => {
  it("carries seconds into minutes", () => {
    expect(formatDuration(119_600, "de")).toBe("2 min 0 s");
    expect(formatDuration(61_000, "de")).toBe("1 min 1 s");
  });

  it("formats token counts in the UI language", () => {
    expect(formatTokens(1234, 5, "de")).toBe("1.234 / 5");
    expect(formatTokens(1234, 5, "en")).toBe("1,234 / 5");
  });
});
