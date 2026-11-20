import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./app";
import { NeonAuthProvider } from "./auth-provider";
import "./i18n";
import "./styles.css";

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <NeonAuthProvider>
          <App />
        </NeonAuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
