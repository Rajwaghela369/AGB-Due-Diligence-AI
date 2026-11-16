import { Moon, Scale, Sun } from "lucide-react";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Link, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { useHotkeys } from "react-hotkeys-hook";

import { HealthIndicator } from "./components/health-indicator";
import { HomePage } from "./features/home/home-page";
import { ReviewWorkspace } from "./features/review/workspace";
import { UploadPage } from "./features/upload/upload-page";
import { Button } from "./components/ui/button";
import { useUiStore } from "./store/ui";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="upload" element={<UploadPage />} />
        <Route path="reviews/:id" element={<ReviewWorkspace />} />
        <Route path="404" element={<PlaceholderPage name="notFound" />} />
        <Route path="*" element={<Navigate replace to="/404" />} />
      </Route>
    </Routes>
  );
}

function AppShell() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const dark = useUiStore((state) => state.dark);
  const toggleDark = useUiStore((state) => state.toggleDark);
  useHotkeys("g,h", () => navigate("/"));
  useHotkeys("g,u", () => navigate("/upload"));

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-4">
          <Link className="flex items-center gap-2 text-lg font-semibold" to="/">
            <Scale aria-hidden="true" className="text-accent" size={22} />
            {t("app.name")}
          </Link>
          <nav aria-label={t("nav.home")} className="flex items-center gap-1">
            <Button asChild variant="ghost">
              <Link to="/">{t("nav.home")}</Link>
            </Button>
            <Button asChild variant="ghost">
              <Link to="/upload">{t("nav.upload")}</Link>
            </Button>
          </nav>
          <div className="ml-auto flex items-center gap-4">
            <HealthIndicator />
            <Button
              aria-label={t("app.theme")}
              onClick={toggleDark}
              size="icon"
              type="button"
              variant="ghost"
            >
              {dark ? <Sun aria-hidden="true" size={18} /> : <Moon aria-hidden="true" size={18} />}
            </Button>
          </div>
        </div>
        <div className="border-t border-border bg-disclaimer px-6 py-2 text-center text-sm">
          {t("app.disclaimer")}
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-12">
        <Outlet />
      </main>
    </div>
  );
}

function PlaceholderPage({ name }: { name: "notFound" }) {
  const { t } = useTranslation();
  return (
    <section className="max-w-2xl rounded-xl border border-border bg-surface p-8 shadow-sm">
      <h1 className="text-3xl font-semibold tracking-tight">{t(`pages.${name}.title`)}</h1>
    </section>
  );
}
