import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleX, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { api, type ApiClient } from "../api/client";

export function HealthIndicator({ client = api }: { client?: ApiClient }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ["health"],
    queryFn: () => client.health(),
    refetchInterval: 10_000,
    retry: false,
  });

  const status = query.isPending ? "checking" : query.isSuccess ? "reachable" : "unreachable";
  const Icon =
    status === "checking" ? LoaderCircle : status === "reachable" ? CircleCheck : CircleX;

  return (
    <div
      aria-label={t("health.label")}
      className="flex items-center gap-2 text-sm text-muted-foreground"
      role="status"
    >
      <Icon aria-hidden="true" className={cnStatus(status)} size={16} strokeWidth={2} />
      <span>{t(`health.${status}`)}</span>
    </div>
  );
}

function cnStatus(status: "checking" | "reachable" | "unreachable") {
  if (status === "reachable") return "text-valid";
  if (status === "unreachable") return "text-risk";
  return "animate-spin text-muted-foreground";
}
