import { LogIn, LogOut, Moon, Scale, Sun } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { useHotkeys } from "react-hotkeys-hook";

import { HealthIndicator } from "./components/health-indicator";
import { HomePage } from "./features/home/home-page";
import { ReviewWorkspace } from "./features/review/workspace";
import { UploadPage } from "./features/upload/upload-page";
import { Button } from "./components/ui/button";
import { useUiStore } from "./store/ui";
import { useAuth } from "./auth-context";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="auth" element={<AuthPage />} />
        <Route element={<ProtectedRoute />}>
          <Route index element={<HomePage />} />
          <Route path="upload" element={<UploadPage />} />
          <Route path="reviews/:id" element={<ReviewWorkspace />} />
        </Route>
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
  const auth = useAuth();
  useHotkeys("g,h", () => navigate("/"));
  useHotkeys("g,u", () => navigate("/upload"));

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-surface/95 shadow-[0_1px_0_rgba(255,255,255,0.02)]">
        <div className="mx-auto flex min-h-[4.5rem] max-w-[100rem] items-center gap-8 px-6 lg:px-8">
          <Link className="flex items-center gap-3 text-lg font-semibold tracking-tight" to="/">
            <span className="flex size-9 items-center justify-center rounded-xl bg-accent/10">
              <Scale aria-hidden="true" className="text-accent" size={21} />
            </span>
            <span>{t("app.name")}</span>
          </Link>
          <nav
            aria-label={t("nav.home")}
            className="flex items-center gap-1 rounded-lg bg-background/60 p-1"
          >
            <Button asChild className="px-3 text-sm" variant="ghost">
              <Link to="/">{t("nav.home")}</Link>
            </Button>
            <Button asChild className="px-3 text-sm" variant="ghost">
              <Link to="/upload">{t("nav.upload")}</Link>
            </Button>
          </nav>
          <div className="ml-auto flex items-center gap-2 lg:gap-4">
            {auth.configured && <AuthControl />}
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
        <div className="border-t border-border bg-disclaimer px-6 py-2 text-center text-xs tracking-wide text-muted-foreground">
          {t("app.disclaimer")}
        </div>
      </header>
      <main className="mx-auto max-w-[100rem] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <Outlet />
      </main>
    </div>
  );
}

function ProtectedRoute() {
  const auth = useAuth();
  if (!auth.configured || auth.pending || auth.session) return <Outlet />;
  return <Navigate replace to="/auth" />;
}

function AuthControl() {
  const { session, signOut } = useAuth();
  const navigate = useNavigate();
  if (!session) {
    return (
      <Button asChild size="default" variant="ghost">
        <Link to="/auth">
          <LogIn aria-hidden="true" size={16} />
          Sign in
        </Link>
      </Button>
    );
  }
  return (
    <Button
      onClick={() => void signOut().then(() => navigate("/auth"))}
      size="default"
      type="button"
      variant="ghost"
    >
      <LogOut aria-hidden="true" size={16} />
      {session.user.name || session.user.email || "Sign out"}
    </Button>
  );
}

function AuthPage() {
  const { configured, pending, session, signIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (!configured) return <Navigate replace to="/" />;
  if (session) return <Navigate replace to="/" />;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      await signIn(email, password);
      navigate("/");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed");
    }
  }

  return (
    <section className="mx-auto max-w-md rounded-xl border border-border bg-surface p-8 shadow-sm">
      <h1 className="text-3xl font-semibold tracking-tight">Sign in</h1>
      <p className="mt-2 text-muted-foreground">Use your Neon Auth account to continue.</p>
      <form className="mt-6 space-y-4" onSubmit={submit}>
        <label className="block text-sm font-medium">
          Email
          <input
            className="mt-1 w-full rounded-md border border-border bg-background p-2"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
        </label>
        <label className="block text-sm font-medium">
          Password
          <input
            className="mt-1 w-full rounded-md border border-border bg-background p-2"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
        </label>
        {error && (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
        <Button disabled={pending} type="submit">
          {pending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </section>
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
