import type { Pricing } from "../../api/types.gen";

/** Keeps accounting figures consistent wherever a review or verdict is shown. */
export function clauseCostUsd(
  pricing: Pricing | undefined,
  inputTokens: number | undefined,
  outputTokens: number | undefined,
): number {
  return (
    ((inputTokens ?? 0) * (pricing?.input_usd_per_million ?? 0) +
      (outputTokens ?? 0) * (pricing?.output_usd_per_million ?? 0)) /
    1_000_000
  );
}

export function formatMoney(
  usd: number | undefined,
  eur: number | null | undefined,
  pricing: Pricing | undefined,
  language: string,
): string | null {
  if (!pricing?.confirmed || usd === undefined) return null;
  if (
    eur !== null &&
    eur !== undefined &&
    pricing.eur_per_usd !== null &&
    pricing.eur_per_usd !== undefined
  ) {
    return new Intl.NumberFormat(language, { style: "currency", currency: "EUR" }).format(eur);
  }
  return new Intl.NumberFormat(language, { style: "currency", currency: "USD" }).format(usd);
}

export function formatDuration(milliseconds: number | null | undefined, language: string): string {
  if (milliseconds === undefined || milliseconds === null) return "–";
  if (milliseconds < 1000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) {
    return (
      new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(milliseconds / 1000) +
      " s"
    );
  }
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.round((milliseconds % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

export function formatTokens(
  inputTokens: number | undefined,
  outputTokens: number | undefined,
): string {
  return `${inputTokens ?? 0} / ${outputTokens ?? 0}`;
}
