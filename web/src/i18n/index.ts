/** German is initialized eagerly so no untranslated shell flashes on first paint. */

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import chatDe from "./chat.de.json";
import chatEn from "./chat.en.json";
import de from "./de.json";
import deliverableDe from "./deliverable.de.json";
import deliverableEn from "./deliverable.en.json";
import en from "./en.json";

// Phase 8 slices keep their strings in their own files, so parallel work never edits the
// same JSON: `chat.*` (rewrite chat) and `summary.*`, `cost.*`, `export.*` (summary block,
// cost and export). The keys stay in the one `translation` namespace.
const resources = {
  de: { translation: { ...de, ...deliverableDe, chat: chatDe } },
  en: { translation: { ...en, ...deliverableEn, chat: chatEn } },
};

void i18n.use(initReactI18next).init({
  resources,
  lng: "de",
  fallbackLng: "de",
  interpolation: { escapeValue: false },
});

export default i18n;
