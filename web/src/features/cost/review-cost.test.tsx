import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Review } from "../../api/types.gen";
import "../../i18n";
import { ReviewCost } from "./review-cost";

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
});
